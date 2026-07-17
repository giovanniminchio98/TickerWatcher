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
cadence (kept low, every ~3.5-4h, to control cost even on Sonnet 5 at full
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
docstring) -- instead, each post's second_part field (Claude's own
per-post call, nudged by how long it's been since the last one that used
it -- see config's second_part_every_n_posts) decides whether the post
gets a genuine continuation posted immediately as a reply, when the topic
has real depth worth adding. Most posts stay a single tweet. This account's
own profile is meant to be enough to inform a reader end to end on X, with
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
    """The real ceiling for this window -- not pacing-adjusted. Night is a
    small flat cap (a couple of 'magnet' posts, no pacing needed at that
    volume). Day is day_max_posts, plus a small reserve unlocked only for
    JUST IN/BREAKING posts -- genuinely urgent news shouldn't wait on a cap
    built for routine content."""
    if kind == "night":
        return cfg.get("night_max_posts", 3)
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


def _ready_for_call(ctx, cfg, state):
    if state["calls_today"] >= cfg["max_calls_per_day"]:
        return False
    last_call = state.get("last_call_time")
    if last_call is None:
        return True
    hours_since = (ctx.now.timestamp() - last_call) / 3600
    # small random jitter so the cadence isn't perfectly clockwork
    required_gap = cfg["min_hours_between_calls"] + random.uniform(0, 0.5)
    return hours_since >= required_gap


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


def _salient_numbers(text):
    return {m.group().lower().replace(" ", "") for m in _SALIENT_NUMBER_RE.finditer(text)}


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


# Confirmed live, verbatim: a batch's reasoning field read "...the recent
# posts list explicitly includes this exact story already... This is a
# duplicate and should NOT be posted" -- and should_post was still true for
# that same item. Claude's own reasoning correctly diagnosed the problem
# and then contradicted itself in the very next field. When the reasoning
# text says this plainly, trust it over the boolean.
_NEGATIVE_REASONING_PHRASES = (
    "should not be posted",
    "should not post",
    "shouldn't be posted",
    "should not have been posted",
    "is a duplicate",
    "this is a duplicate",
    "already posted",
    "already covered",
    "was already covered",
    "not be posted",
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


def _send_run_summary(state, reason, posted_this_run):
    """One short bot-chat-only line every run (never the public channel),
    regardless of what happened -- separate from _send_audit_message (which
    only fires when a new batch is actually generated) and from
    _drain_queue's own per-post line. Lets you tell at a glance whether this
    run made a new Claude call or not (and why not), whether it posted
    anything, and how many items are already queued for the next run(s) to
    drain automatically -- so a quiet run reads as "expected, nothing due"
    rather than leaving you to guess."""
    queue_len = len(state.get("post_queue", []))
    post_label = "posted" if posted_this_run else "no post"
    telegram_client.send_message(f"🤖 AI Manager: {reason} · {post_label} · queue: {queue_len} left")


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
    text = _enforce_single_cashtag(truncate(_enforce_opening_tag(item["text"]), ai_manager_brain.MAX_POST_LEN))
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
    channel_text = f"{text}\n\n{second_part}" if second_part else text
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
    state["posts_since_last_second_part"] = (
        0 if has_second_part else state.get("posts_since_last_second_part", 0) + 1
    )
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
    state.setdefault("posts_since_last_second_part", 0)
    state.setdefault("window_id", None)
    state.setdefault("window_posts", 0)

    window_id, window_kind, window_start, window_end = _current_window(ctx, cfg)
    _roll_window(state, window_id)

    fired = _drain_queue(ctx, cfg, state, window_kind, window_start, window_end)

    # only generate a new batch once the queue is empty and it's actually
    # time for a new Claude call -- this is what keeps
    # Claude spend near today's cadence even though visible posting cadence
    # is much higher via the queue
    if state["post_queue"]:
        _send_run_summary(state, "queue still draining, no new call", fired)
        return fired
    if not _ready_for_call(ctx, cfg, state):
        _send_run_summary(state, "no new call (cooldown/daily cap)", fired)
        return fired
    if not ctx.claude_budget.can_spend():
        logger.info("ai_manager: Claude budget exhausted this month, skipping call")
        _send_run_summary(state, "no new call (Claude budget capped)", fired)
        return fired

    snapshot = {
        "prices": _price_snapshot_lines(ctx),
        "news": _news_snapshot(ctx),
        "earnings": _earnings_snapshot(ctx),
        "press_releases": _press_releases_snapshot(ctx),
        "own_recent_posts": story_history.recent_texts(ctx.state, ctx.now.timestamp()),
        "filler_examples": _filler_examples(ctx),
        "posts_per_batch": cfg.get("posts_per_batch", 1),
        "second_part_every_n_posts": cfg.get("second_part_every_n_posts", 4),
        "posts_since_last_second_part": state.get("posts_since_last_second_part", 0),
    }

    decision, usage = ai_manager_brain.decide(snapshot, cfg["model"])
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        # outright API failure or an unparseable response -- don't start the
        # cadence cooldown on a call that produced nothing usable, so the
        # very next hourly run retries immediately instead of waiting out a
        # full cooldown for a call that never actually happened. calls_today
        # still increments either way, so a persistently broken call can't
        # retry more than max_calls_per_day times in one day.
        _send_run_summary(state, "new call failed/unparsed, will retry next run", fired)
        return fired

    # only a successfully parsed decision starts the real cooldown
    state["last_call_time"] = ctx.now.timestamp()

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
    already_posted_texts = snapshot["own_recent_posts"]
    for post in (decision.get("posts") or [])[: cfg.get("posts_per_batch", 1)]:
        if not post.get("should_post") or not post.get("text"):
            declined_posts.append(post)
            continue
        # deterministic overrides: even if Claude set should_post true,
        # don't trust it blindly -- catches both a Claude reasoning field
        # that plainly contradicts its own should_post (confirmed live) and
        # a likely-duplicate topic by shared distinctive figures
        if _reasoning_contradicts_post(post.get("reasoning")):
            logger.warning(
                "ai_manager: declining post whose own reasoning contradicts should_post=true: %s",
                post.get("reasoning", "")[:120],
            )
            declined_posts.append(post)
            continue
        prior_texts = already_posted_texts + [item["text"] for item in queued_items]
        if _is_likely_duplicate(post["text"], prior_texts):
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

    _send_run_summary(state, f"new batch ({len(queued_items)} queued)", fired)
    return fired
