"""Post type 11 (opt-in via ANTHROPIC_API_KEY presence): autonomous
original-post decision-maker, meant to run unattended for months,
targeting close to one post per hourly run. See
src/sources/ai_manager_brain.py for the prompt/parsing and README's "AI
Manager" section for the full design rationale.

Each Claude call produces a BATCH of up to posts_per_batch posts (see
config/ai_manager.json) instead of just one -- the first is fired
immediately, any others are queued in state["ai_manager"]["post_queue"]
and drained one item per subsequent run, relying on main.py's hourly cron
cadence to naturally spread them out over the following hours. This is
what decouples the visible posting cadence (high) from the Claude call
cadence (fixed, every 3h on a clock checkpoint -- see
_CALL_CHECKPOINT_HOURS -- to control cost even on Sonnet 5 at full
post-intro pricing) -- see ai_manager_brain.py's docstring for the cost
reasoning. A queued post older than max_queue_age_hours is dropped rather
than fired stale.

Posting is capped by a day/night window (Europe/Brussels time, see
_current_window): a larger day_max_posts cap during day_start_hour-
day_end_hour, a small night_max_posts cap overnight (a couple of high-value
"magnet" posts, not a real cadence), and a small day_tag_exception_reserve
on top of the day cap reserved for genuinely urgent JUST IN/BREAKING posts
only. Within the day window, posts are also paced to spread evenly across
the whole window rather than exhausting the cap by mid-morning and leaving
the rest of the day silent (_paced_cap_for) -- this gate applies both when
queuing a fresh batch and when draining the queue, so an over-queued batch
still spreads out naturally one item per hourly run.

Replies used to live in this same call but now run on their own, much
faster cadence in reply_manager.py -- see that module's docstring for why.
Reposting (retweet/quote-tweet) used to live here too -- removed entirely
by explicit choice: reposts are now a manual, human decision only, so this
trigger never touches X's retweet/quote endpoints.

No images, no links on X, by deliberate choice (see ai_manager_brain.py's
docstring) -- instead, every post's second_part field is now mandatory
(Claude must always fill it in, see ai_manager_brain.py's prompt): a
reply posted immediately after the main post whose one job is explaining
the news and its meaning in clear, simple terms. This account's own
profile is meant to be enough to inform a reader end to end on X, with
no outbound clicks needed there.

Telegram is the one exception: when a post is based on a specific news
article (Claude's news_index), the channel copy always shows that
article's real source link (`_preferred_link`) -- X itself still never
carries a link here. Same reasoning already used elsewhere in this
codebase (Telegram is free, so it can be more generous than the X post).

Two independent hard budget caps gate this trigger, each stopping it
cleanly rather than erroring when exhausted:
  - ctx.claude_budget (config/claude_budget.json) -- gates whether a new
    batch-generating Claude call is even attempted (queue draining never
    needs this, since it doesn't call Claude).
  - ctx.budget (config/budget.json) -- gates whether a decided post/
    second_part is actually sent to X (same shared pool every other
    trigger uses).

Every batch-generating call sends one Telegram bot-chat audit message
(all queued posts + reasoning, or "no action") -- with no manual approval
step, this is the only way to spot-check the underlying judgment over
time. Each individual post firing (whether the first-in-batch or a later
queue drain) also gets its own short bot-chat line, plus the existing
per-post cost-chat notification from ctx.budget.record_spend(), plus one
per-run status line (see _send_run_summary) summarizing whether a new
call happened this run and how many posts are still queued.
"""
import logging
import math
import random
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from src import story_history, telegram_client
from src.formatting import fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain, news_rss, twelvedata

CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")

# Only these two TAGS count as "urgent" for the day-window tag exception --
# CONTEXT/CRYPTO/AI/NEWS are routine content, not real-news bulletins worth
# breaking pacing/cap for.
_URGENT_TAGS = ("🚨 JUST IN", "🚨 BREAKING")

# A dollar figure with a scale word, or a percentage -- distinctive enough
# that two unrelated real stories sharing 2+ of them verbatim is rare.
_SALIENT_NUMBER_RE = re.compile(
    r"\$\s?\d[\d,.]*\s?(?:million|billion|trillion|M|B|K)\b|\d+(?:\.\d+)?%",
    re.IGNORECASE,
)

logger = logging.getLogger("tickerwatch.triggers.ai_manager")


def _filler_examples(ctx, n=5):
    """A small random sample from config/filler.json's generic engagement
    prompts, handed to Claude as style reference only (never posted
    verbatim) for the rare case nothing data-driven is post-worthy but a
    genuine, non-rubbish generic post would still be nice -- see
    ai_manager_brain._build_prompt's "GENERIC ENGAGEMENT EXAMPLES" section.
    Absorbs filler.py's old role of "always post something," but only as an
    option Claude can take or leave, not a mechanical last resort."""
    posts = ctx.config.get("filler", {}).get("posts", [])
    if not posts:
        return []
    return random.sample(posts, min(n, len(posts)))


def _roll_day(state, today_str):
    """Rolls over the Claude-call cadence counter only -- calendar-day/UTC
    based, since max_calls_per_day is about controlling Claude spend, not
    posting cadence. The posting cap itself is windowed (day/night, Brussels
    time) and rolled separately by _roll_window, since the night window
    spans across a UTC midnight."""
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0


def _current_window(ctx, cfg):
    """Identifies the day or night posting window 'now' falls into, in
    Europe/Brussels time (same zoneinfo pattern as budget_report.py).
    Returns (window_id, kind, start, end): window_id is a stable string
    identifying this specific window instance (e.g. "2026-07-17-day") so a
    transition into a new window -- including the night window, which spans
    across midnight -- can be detected and counters reset accordingly."""
    now = ctx.now.astimezone(BRUSSELS_TZ)
    day_start_hour = cfg.get("day_start_hour", 6)
    day_end_hour = cfg.get("day_end_hour", 23)

    if day_start_hour <= now.hour < day_end_hour:
        kind = "day"
        start = now.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
        end = now.replace(hour=day_end_hour, minute=0, second=0, microsecond=0)
        window_date = now.date()
    elif now.hour >= day_end_hour:
        kind = "night"
        start = now.replace(hour=day_end_hour, minute=0, second=0, microsecond=0)
        end = (now + timedelta(days=1)).replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
        window_date = now.date()
    else:
        kind = "night"
        start = (now - timedelta(days=1)).replace(hour=day_end_hour, minute=0, second=0, microsecond=0)
        end = now.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
        window_date = now.date() - timedelta(days=1)

    window_id = f"{window_date.isoformat()}-{kind}"
    return window_id, kind, start, end


def _roll_window(state, window_id):
    if state.get("window_id") != window_id:
        state["window_id"] = window_id
        state["window_posts"] = 0


def _is_urgent(text):
    stripped = text.lstrip()
    return any(stripped.startswith(f"{tag}:") for tag in _URGENT_TAGS)


def _window_hard_cap(kind, cfg, urgent):
    """The real ceiling for this window -- not pacing-adjusted. Both windows
    get a small reserve on top of their base cap, unlocked only for JUST IN/
    BREAKING posts -- genuinely urgent, extremely-relevant news should never
    be held back by a cap built for routine content, day or night."""
    if kind == "night":
        base = cfg.get("night_max_posts", 3)
        if urgent:
            return base + cfg.get("night_tag_exception_reserve", 2)
        return base
    base = cfg.get("day_max_posts", 10)
    if urgent:
        return base + cfg.get("day_tag_exception_reserve", 2)
    return base


def _paced_cap_for(kind, cfg, start, end, now, urgent):
    """How many posts are allowed so far into the window, given even-spread
    pacing -- day window only, and only for non-urgent posts. Confirmed live
    that without this, a burst of Claude calls could exhaust the whole day's
    cap by mid-morning, leaving the rest of the day silent. A small grace
    period (day_pacing_grace_hours) is added to elapsed time so the very
    start of the window isn't stuck at an allowance of 0 -- without it,
    nothing could post right at day_start_hour until real time had passed."""
    hard_cap = _window_hard_cap(kind, cfg, urgent)
    if kind == "night" or urgent:
        return hard_cap
    total_hours = (end - start).total_seconds() / 3600
    if total_hours <= 0:
        return hard_cap
    elapsed_hours = max(0.0, (now - start).total_seconds() / 3600)
    grace_hours = cfg.get("day_pacing_grace_hours", 2)
    fraction = min(1.0, (elapsed_hours + grace_hours) / total_hours)
    return min(hard_cap, math.ceil(fraction * hard_cap))


def _day_context(ctx):
    """A plain-English line telling Claude what day it actually is, so it
    can phrase STOCK price timing correctly -- confirmed live that a post
    said stocks moved 'today' on a weekend, when US markets are closed and
    that move actually happened in Friday's session. Crypto trades 24/7 so
    it never has this problem; this is purely a US-stock-market framing
    issue. Simple weekday/weekend check (via ctx.now, not US-Eastern-exact)
    -- doesn't account for market holidays, but that's a much rarer edge
    case than every single weekend."""
    day_name = ctx.now.strftime("%A, %B %d, %Y")
    if ctx.now.weekday() >= 5:  # Saturday=5, Sunday=6
        return (
            f"{day_name} -- a weekend. US stock markets are closed; any stock price move "
            "reflects the last trading session (Friday), not something that happened today. "
            "Crypto trades 24/7 and is unaffected."
        )
    return f"{day_name} -- a weekday. US stock markets are open during their normal trading hours."


# Fixed clock checkpoints (Europe/Brussels, matching every other time-of-day
# decision in this module) instead of a rolling "N hours since last call"
# gate -- confirmed live that the old elapsed-time+jitter approach drifted
# over time and produced uneven gaps, reading as "long stretches of nothing
# then a burst." Every 3 hours, on the hour, every day: 00/03/06/09/12/15/
# 18/21. Deliberately no jitter this time -- the whole point is now
# predictable spacing, not avoiding a clockwork pattern.
_CALL_CHECKPOINT_HOURS = (0, 3, 6, 9, 12, 15, 18, 21)


def _ready_for_call(ctx, cfg, state):
    if state["calls_today"] >= cfg["max_calls_per_day"]:
        return False
    brussels_now = ctx.now.astimezone(BRUSSELS_TZ)
    if brussels_now.hour not in _CALL_CHECKPOINT_HOURS:
        return False
    # guards against firing twice for the same checkpoint if a run somehow
    # executes more than once within that hour (retry, manual trigger, etc.)
    checkpoint_id = brussels_now.strftime("%Y-%m-%d-%H")
    return state.get("last_call_checkpoint") != checkpoint_id


def _price_snapshot_lines(ctx):
    """Crypto comes from ctx.prices (already fetched once per run by
    main.py). Stocks use watchlist.stocks_broad (30 tickers) via
    twelvedata.get_quotes_batch -- chunked into 5-symbol requests with a
    full 60s pause between chunks (confirmed live that a shorter 15s
    pause still hit Twelve Data's free-tier per-minute limit). Adds ~5
    minutes to a call that needs it, deliberately accepted to get real
    stock data reliably rather than dropping the feature. Never raises,
    so no try/except needed here -- a symbol just won't have a line if
    its chunk didn't come through. Falls back to the smaller 'stocks'
    list if stocks_broad isn't configured."""
    lines = []
    for asset in ctx.config["watchlist"]["crypto"]:
        info = ctx.prices.get(asset["coingecko_id"])
        if not info or info.get("usd") is None:
            continue
        lines.append(
            f"{asset['symbol']}: ${fmt_price(info['usd'])} ({fmt_pct(info.get('usd_24h_change'))} 24h)"
        )

    stocks = ctx.config["watchlist"].get("stocks_broad") or ctx.config["watchlist"].get("stocks", [])
    quotes = twelvedata.get_quotes_batch([asset["symbol"] for asset in stocks])
    for asset in stocks:
        q = quotes.get(asset["symbol"])
        if q:
            lines.append(f"{asset['symbol']}: ${fmt_price(q['price'])} ({fmt_pct(q['percent_change'])})")
    return lines


def _oracle_snapshot_lines(ctx):
    """One line per tracked coin summarizing this run's CryptoScope Oracle
    read (ctx.oracle, computed fresh every run by main.py's
    _fetch_oracle_data from live Binance price history -- see
    src/sources/cryptoscope_oracle.py) -- a real statistical signal, not a
    fabricated number, so Claude can weigh it like any other real data
    point in the prompt. Coins with too little candle history yet
    (analyze() returned None) are simply omitted rather than padded with
    a placeholder line."""
    lines = []
    for asset in ctx.config["watchlist"]["crypto"]:
        result = ctx.oracle.get(asset["symbol"])
        if not result:
            continue
        composite = result["composite"]
        probs = result["probs"]
        lines.append(
            f"{asset['symbol']}: {composite['label']} (score {composite['score']}/100, "
            f"{composite['confidence']}% confidence) -- {result['regime']['label']}; "
            f"{round(probs['p_up'] * 100)}% odds up over the next {result['meta']['horizon']}h, "
            f"median move {fmt_pct(probs['med_ret'] * 100)}"
        )
    return lines


def _earnings_snapshot(ctx):
    """Today's earnings calendar (Twelve Data, free-tier endpoint), scoped
    to watchlist.stocks_broad -- gives Claude a real, timely "X reports
    earnings today" angle independent of price moves. Same
    try/except-and-default-to-empty pattern as every other external call
    in this module."""
    try:
        entries = twelvedata.get_earnings_calendar()
    except Exception:
        logger.exception("Twelve Data earnings_calendar fetch failed for ai_manager")
        return []
    tracked = {asset["symbol"] for asset in ctx.config["watchlist"].get("stocks_broad", [])}
    return [e for e in entries if e.get("symbol") in tracked]


def _press_releases_snapshot(ctx, max_results=10):
    """Recent official press releases (Twelve Data, free-tier endpoint)
    for watchlist.stocks_broad -- a primary-source angle distinct from the
    RSS/journalism news already used elsewhere. Same
    try/except-and-default-to-empty pattern as everything else here."""
    symbols = [asset["symbol"] for asset in ctx.config["watchlist"].get("stocks_broad", [])]
    try:
        return twelvedata.get_press_releases(symbols, max_results=max_results)
    except Exception:
        logger.exception("Twelve Data press_releases fetch failed for ai_manager")
        return []


def _news_snapshot(ctx, limit=6):
    """Excludes articles already covered (by ANY trigger, not just this
    one -- see src/story_history.py) within the shared rolling window.
    Confirmed live that without cross-trigger sharing, the same real story
    could resurface within hours via news_alerts.py even after ai_manager
    already covered it, or vice versa, since each trigger only ever
    checked its own separate dedup list. Not a permanent block: the window
    rolls, so a story is fair game again once it's aged out (a different
    day/week revisit is fine, even good)."""
    kw_cfg = ctx.config["keywords"]
    already_used = story_history.recent_urls(ctx.state, ctx.now.timestamp())
    try:
        return news_rss.fetch_matching_articles(kw_cfg["rss_feeds"], kw_cfg["keywords"], already_used, limit)
    except Exception:
        logger.exception("News fetch failed for ai_manager")
        return []


def _preferred_link(snapshot, post):
    """The real source URL of the news article this post is actually based
    on (post['news_index']), if there is one -- Telegram-only (see module
    docstring), X never carries a link here regardless."""
    idx = post.get("news_index")
    if idx is not None and isinstance(idx, int) and 0 <= idx < len(snapshot["news"]):
        return snapshot["news"][idx]["url"]
    return None


def _enforce_single_cashtag(text):
    """X hard-rejects (403 Forbidden) any single post with more than one
    $cashtag -- confirmed live (a post naming both $STRF and $STRC failed
    to send entirely). Keeps the first cashtag intact (genuinely nice to
    have: free, and X renders it with a live price card) and strips just
    the leading '$' from any additional ones, so the post still reads
    naturally instead of failing to send at all."""
    matches = list(CASHTAG_RE.finditer(text))
    if len(matches) <= 1:
        return text
    parts = []
    last_end = 0
    for i, m in enumerate(matches):
        parts.append(text[last_end:m.start()])
        parts.append(m.group() if i == 0 else m.group()[1:])
        last_end = m.end()
    parts.append(text[last_end:])
    return "".join(parts)


# Unifies spelled-out scale words with their abbreviation so "$400 million"
# and "$400M" normalize to the identical token -- confirmed live that a
# duplicate post slipped through _is_likely_duplicate because the original
# story used spelled-out units ("$400 million... $20 billion") while the
# repeat used abbreviations ("$400M... $20B"): naive lowercase+strip-spaces
# treated "$400million" and "$400m" as two unrelated numbers.
_UNIT_WORD_TO_ABBREV = (("million", "m"), ("billion", "b"), ("trillion", "t"), ("thousand", "k"))


def _salient_numbers(text):
    numbers = set()
    for m in _SALIENT_NUMBER_RE.finditer(text):
        normalized = m.group().lower().replace(" ", "")
        for word, abbrev in _UNIT_WORD_TO_ABBREV:
            normalized = normalized.replace(word, abbrev)
        numbers.add(normalized)
    return numbers


def _is_likely_duplicate(text, prior_texts, min_shared=2):
    """Deterministic backstop against the same real-world story getting
    posted twice -- confirmed live that Claude can independently regenerate
    a post covering a topic already sitting right there in its own
    RECENTLY POSTED context, despite the explicit prompt rule against it
    ("should_post: false since this was already covered" is a request to
    Claude, not a guarantee -- same reasoning as every other code-level
    backstop in this module: cashtag, opening tag). Flags a likely
    duplicate when a candidate shares min_shared+ distinctive figures (a
    dollar amount with a scale word, or a percentage) verbatim with any
    already-posted text -- two unrelated real stories coincidentally
    sharing two exact figures is rare enough that this stays low on false
    positives while catching the actual observed failure (the same
    "$400 million... $20 billion" Citadel/Crypto.com story posted twice
    within one Claude batch's own recent-post window)."""
    candidate_nums = _salient_numbers(text)
    if len(candidate_nums) < min_shared:
        return False
    for prior in prior_texts:
        if len(candidate_nums & _salient_numbers(prior)) >= min_shared:
            return True
    return False


# Confirmed live, verbatim, twice now, with different exact phrasing each
# time -- a batch's reasoning field correctly diagnosed the problem and then
# contradicted itself in the very next field (should_post stayed true):
#   1) "...This is a duplicate and should NOT be posted."
#   2) "Wait -- Citadel/Crypto.com has already been posted twice in recent
#      history... Skipping this to avoid repetition." -- this one slipped
#      through the original phrase list entirely ("already been posted" vs.
#      the listed "already posted", and "skipping this" wasn't listed at
#      all), while still sitting in should_post=true and getting queued.
# When the reasoning text says this plainly, trust it over the boolean.
_NEGATIVE_REASONING_PHRASES = (
    "should not be posted",
    "should not post",
    "shouldn't be posted",
    "should not have been posted",
    "is a duplicate",
    "this is a duplicate",
    "already posted",
    "already been posted",
    "already covered",
    "was already covered",
    "not be posted",
    "skipping this",
    "skip this",
    "to avoid repetition",
    "avoid repeating",
)


def _reasoning_contradicts_post(reasoning):
    lowered = (reasoning or "").lower()
    return any(phrase in lowered for phrase in _NEGATIVE_REASONING_PHRASES)


def _enforce_opening_tag(text):
    """Every main post must open with one of ai_manager_brain.TAGS -- same
    defense-in-depth pattern as _enforce_single_cashtag: the prompt already
    requires this, but a rule stated in a prompt is a request, not a
    guarantee, and a post silently missing its tag breaks the profile's
    visual consistency. If none of the tags is present at the very start,
    prepend a default rather than let it go out untagged -- JUST IN over a
    more "neutral" label like CONTEXT, since this is a rare fallback for an
    otherwise real, timely post (not an evergreen piece that just forgot
    its tag), and it reads as more engaging regardless. Only applies to the
    main post -- second_part is deliberately tag-free."""
    stripped = text.lstrip()
    if any(stripped.startswith(f"{tag}:") for tag in ai_manager_brain.TAGS):
        return text
    return f"🚨 JUST IN: {text}"


def _minutes_to_next_checkpoint(ctx):
    """Exact minutes until the next fixed 3-hour checkpoint (see
    _CALL_CHECKPOINT_HOURS) -- unlike the old elapsed-time+jitter cadence,
    checkpoints are deterministic clock times, so this is an exact
    countdown, not an estimate."""
    brussels_now = ctx.now.astimezone(BRUSSELS_TZ)
    for h in _CALL_CHECKPOINT_HOURS:
        candidate = brussels_now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > brussels_now:
            return int((candidate - brussels_now).total_seconds() // 60)
    # every checkpoint today has passed -- wrap to the first one tomorrow
    tomorrow_first = (brussels_now + timedelta(days=1)).replace(
        hour=_CALL_CHECKPOINT_HOURS[0], minute=0, second=0, microsecond=0
    )
    return int((tomorrow_first - brussels_now).total_seconds() // 60)


def _send_run_summary(ctx, cfg, state, reason, posted_this_run):
    """One short bot-chat-only line every run (never the public channel),
    regardless of what happened -- separate from _send_audit_message (which
    only fires when a new batch is actually generated) and from
    _drain_queue's own per-post line. Lets you tell at a glance whether this
    run made a new Claude call or not (and why not), whether it posted
    anything, and how many items are already queued for the next run(s) to
    drain automatically -- so a quiet run reads as "expected, nothing due"
    rather than leaving you to guess. When the reason is the cooldown gate
    specifically, also shows the exact time to the next 3-hour checkpoint."""
    queue_len = len(state.get("post_queue", []))
    post_label = "posted" if posted_this_run else "no post"
    eta_suffix = ""
    if "cooldown" in reason:
        h, m = divmod(_minutes_to_next_checkpoint(ctx), 60)
        eta_suffix = f" · next call in ~{h}h {m}m" if h else f" · next call in ~{m}m"
    telegram_client.send_message(f"🤖 AI Manager: {reason} · {post_label} · queue: {queue_len} left{eta_suffix}")


def _send_audit_message(queued_items, declined_posts):
    lines = ["🤖 AI Manager batch decision:"]
    if queued_items:
        lines.append(f"\n📝 {len(queued_items)} post(s) queued (spread out over the next few runs):")
        for item in queued_items:
            tag = "two-part" if item.get("second_part") else "single"
            lines.append(f"\n- ({tag}) {item['text']}\nReasoning: {item['reasoning']}")
    else:
        reasons = "; ".join(p.get("reasoning", "") for p in declined_posts) or "(none given)"
        lines.append(f"\n📝 No posts queued this batch. Reasoning: {reasons}")

    telegram_client.send_message("\n".join(lines))


def _drain_queue(ctx, cfg, state, window_kind, window_start, window_end):
    """Fires at most one queued post per run -- the hourly cron cadence
    itself is what spreads a batch's posts across the day, no extra Claude
    call needed. Gated on the day/night window's paced allowance (see
    _paced_cap_for) rather than a flat daily count, so an over-queued batch
    still spreads out naturally: items just wait in the queue, one drain
    attempt per run, until the window's allowance has caught up to them (or
    a genuinely urgent JUST IN/BREAKING item skips pacing entirely). Returns
    True if something was actually posted."""
    queue = state.get("post_queue", [])
    if not queue:
        return False

    max_age = cfg.get("max_queue_age_hours", 12)
    fresh = [
        item for item in queue
        if (ctx.now.timestamp() - item.get("queued_at", ctx.now.timestamp())) / 3600 <= max_age
    ]
    if len(fresh) != len(queue):
        logger.info("ai_manager: dropped %d stale queued post(s)", len(queue) - len(fresh))
    state["post_queue"] = fresh
    if not fresh:
        return False

    urgent = _is_urgent(fresh[0]["text"])
    allowed = _paced_cap_for(window_kind, cfg, window_start, window_end, ctx.now, urgent)
    if state["window_posts"] >= allowed:
        return False
    if not ctx.budget.can_spend(has_link=False):
        return False

    item = state["post_queue"].pop(0)
    # tagged_text is Claude's intended text (tag-enforced, but otherwise
    # exactly what it wrote) -- text is the X-ready version, truncated to
    # MAX_POST_LEN as a last-resort safety net when Claude ran over budget.
    # Confirmed live that truncate()'s flat-cut fallback can read as an ugly
    # mid-thought cutoff when a post runs well over budget with no sentence
    # boundary in the salvageable range -- that's an X-only constraint (280
    # hard tweet limit), not something Telegram needs at all, so the channel
    # copy always uses the untruncated tagged_text instead of text.
    tagged_text = _enforce_opening_tag(item["text"])
    text = _enforce_single_cashtag(truncate(tagged_text, ai_manager_brain.MAX_POST_LEN))
    second_part = item.get("second_part")

    tweet_id = ctx.x.post(text)
    if not tweet_id:
        # ctx.x.post() itself failed -- ops_alerts already fired for this;
        # drop rather than retry indefinitely on a persistently broken post
        telegram_client.send_message(f"⚠️ AI Manager: queued post failed to send, dropped: {text}")
        return False

    # Telegram is free, so the channel copy always includes the second_part
    # too when there is one, right in the same message, plus the real news
    # link when the post is based on one specific article (item["link_url"])
    # -- X gets none of this, it only ever posts plain link-free text.
    channel_text = f"{tagged_text}\n\n{second_part}" if second_part else tagged_text
    link_url = item.get("link_url")
    channel_link = ("Read more", link_url) if link_url else None
    ctx.budget.record_spend(has_link=False, text=text, channel_text=channel_text, channel_link=channel_link)
    if second_part and ctx.budget.can_spend(has_link=False):
        reply_text = _enforce_single_cashtag(truncate(second_part, ai_manager_brain.MAX_POST_LEN))
        reply_id = ctx.x.reply(reply_text, tweet_id)
        if reply_id:
            # already mirrored to the channel above via channel_text, skip duplicate
            ctx.budget.record_spend(has_link=False, text=second_part, mirror_to_channel=False)

    has_second_part = bool(second_part)
    story_history.add_entry(ctx.state, text=text, url=link_url, now_ts=ctx.now.timestamp())
    state["window_posts"] += 1
    telegram_client.send_message(
        f"✅ X post created ({'two-part' if has_second_part else 'single'})"
    )
    return True


def run(ctx):
    cfg = ctx.config["ai_manager"]
    state = ctx.state["ai_manager"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    _roll_day(state, today_str)
    state.setdefault("post_queue", [])
    state.setdefault("window_id", None)
    state.setdefault("window_posts", 0)
    state.setdefault("last_call_checkpoint", None)

    window_id, window_kind, window_start, window_end = _current_window(ctx, cfg)
    _roll_window(state, window_id)

    fired = _drain_queue(ctx, cfg, state, window_kind, window_start, window_end)

    # only generate a new batch once the queue is empty and it's actually
    # time for a new Claude call -- this is what keeps
    # Claude spend near today's cadence even though visible posting cadence
    # is much higher via the queue
    if state["post_queue"]:
        _send_run_summary(ctx, cfg, state, "queue still draining, no new call", fired)
        return fired
    if not _ready_for_call(ctx, cfg, state):
        _send_run_summary(ctx, cfg, state, "no new call (cooldown/daily cap)", fired)
        return fired
    if not ctx.claude_budget.can_spend():
        logger.info("ai_manager: Claude budget exhausted this month, skipping call")
        _send_run_summary(ctx, cfg, state, "no new call (Claude budget capped)", fired)
        return fired

    snapshot = {
        "day_context": _day_context(ctx),
        "prices": _price_snapshot_lines(ctx),
        "oracle": _oracle_snapshot_lines(ctx),
        "news": _news_snapshot(ctx),
        "earnings": _earnings_snapshot(ctx),
        "press_releases": _press_releases_snapshot(ctx),
        "own_recent_posts": story_history.recent_texts(ctx.state, ctx.now.timestamp()),
        "filler_examples": _filler_examples(ctx),
        "posts_per_batch": cfg.get("posts_per_batch", 1),
    }

    decision, usage = ai_manager_brain.decide(snapshot, cfg["model"])
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        # outright API failure or an unparseable response -- don't mark this
        # checkpoint as used on a call that produced nothing usable. The
        # earliest retry is still the next 3-hour checkpoint (not sooner --
        # _ready_for_call requires an aligned checkpoint hour regardless),
        # but at least a persistently broken call doesn't permanently burn
        # today's checkpoint slot. calls_today still increments either way,
        # so repeated failures can't retry more than max_calls_per_day times.
        _send_run_summary(ctx, cfg, state, "new call failed/unparsed, will retry next checkpoint", fired)
        return fired

    # only a successfully parsed decision marks this checkpoint as used
    state["last_call_time"] = ctx.now.timestamp()
    state["last_call_checkpoint"] = ctx.now.astimezone(BRUSSELS_TZ).strftime("%Y-%m-%d-%H")

    # cap what gets queued against the window's HARD cap (not the paced
    # sub-allowance) -- pacing is enforced at drain time instead (see
    # _drain_queue), so an over-queued batch still spreads out one item per
    # hourly run rather than draining back-to-back. already_committed
    # counts what's already posted this window plus what's already sitting
    # in the queue (destined for this window unless it rolls over first).
    already_committed = state["window_posts"] + len(state["post_queue"])
    hard_cap_normal = _window_hard_cap(window_kind, cfg, urgent=False)
    hard_cap_urgent = _window_hard_cap(window_kind, cfg, urgent=True)

    queued_items = []
    declined_posts = []
    # Deliberately NOT snapshot["own_recent_posts"] (capped at 30 for the
    # prompt) -- confirmed live that the same story slipped past this check
    # 3 times over 57 hours because a high-volume day pushed the earlier
    # mentions past that cap before they aged out of the 72h window. This
    # deterministic check is cheap local comparison, not prompt tokens, so
    # it gets the full uncapped window instead -- see story_history.py's
    # recent_texts docstring.
    already_posted_texts = story_history.recent_texts(ctx.state, ctx.now.timestamp(), limit=None)
    for post in (decision.get("posts") or [])[: cfg.get("posts_per_batch", 1)]:
        if not post.get("should_post") or not post.get("text"):
            declined_posts.append(post)
            continue
        # deterministic overrides: even if Claude set should_post true,
        # don't trust it blindly -- catches both a Claude reasoning field
        # that plainly contradicts its own should_post (confirmed live) and
        # a likely-duplicate topic by shared distinctive figures. Checked
        # against second_part too, not just reasoning/text -- confirmed
        # live that with second_part now mandatory (see ai_manager_brain.py),
        # Claude can write its own internal second-guessing straight into
        # the reply itself (e.g. a real posted reply that read "Wait --
        # this was already covered. Skipping to avoid repeat.") instead of
        # a genuine explanation. reasoning never goes to X, but second_part
        # does, so it needs the exact same scrutiny.
        second_part_text = post.get("second_part") or ""
        if _reasoning_contradicts_post(post.get("reasoning")) or _reasoning_contradicts_post(second_part_text):
            logger.warning(
                "ai_manager: declining post whose reasoning or second_part contradicts should_post=true: %s",
                (post.get("reasoning") or second_part_text)[:120],
            )
            declined_posts.append(post)
            continue
        prior_texts = already_posted_texts + [item["text"] for item in queued_items]
        if _is_likely_duplicate(post["text"], prior_texts) or (
            second_part_text and _is_likely_duplicate(second_part_text, prior_texts)
        ):
            logger.warning(
                "ai_manager: declining likely-duplicate post (shared salient figures with a recent post): %s",
                post["text"][:80],
            )
            declined_posts.append(post)
            continue
        urgent = _is_urgent(post["text"])
        cap = hard_cap_urgent if urgent else hard_cap_normal
        if already_committed + len(queued_items) >= cap:
            logger.info("ai_manager: declining post, window hard cap reached (urgent=%s)", urgent)
            declined_posts.append(post)
            continue
        link_url = _preferred_link(snapshot, post)
        queued_items.append({
            "text": post["text"],
            "second_part": post.get("second_part"),
            "link_url": link_url,
            "reasoning": post.get("reasoning", ""),
            "queued_at": ctx.now.timestamp(),
        })
    state["post_queue"].extend(queued_items)

    _send_audit_message(queued_items, declined_posts)

    # fire the first queued item right away rather than waiting for the next
    # hourly tick, so a fresh batch doesn't just sit until then -- still
    # subject to the same paced allowance check inside _drain_queue
    if state["post_queue"]:
        fired = _drain_queue(ctx, cfg, state, window_kind, window_start, window_end) or fired

    _send_run_summary(ctx, cfg, state, f"new batch ({len(queued_items)} queued)", fired)
    return fired
