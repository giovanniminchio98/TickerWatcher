"""Post type 11 (opt-in via ANTHROPIC_API_KEY presence): a 3x/day
(06:00/12:00/21:00 Brussels -- see _CALL_CHECKPOINT_HOURS) world-news
recap. Each call decides a BATCH of 0 to max_posts_per_call posts (config/
ai_manager.json, default 4) -- a broad snapshot of the most important
things that happened since the last recap, world news first, crypto/
finance/AI folded in only when genuinely notable. See
src/sources/ai_manager_brain.py for the prompt/parsing.

Replaced the old design entirely (2026-07-20): a batch of up to
posts_per_batch individually-decided posts on a fixed 3-hour/8-checkpoint
clock, queued and drained one item per hourly run, weighted toward
routine crypto/price content with no genuine world-news source feeding it
at all. The account owner's own honest read: they wouldn't reliably
follow most of what that produced. This is a full pivot, not a tweak --
"3x/day, world news as the primary lens" is a different shape of account,
not the same one turned down. Refined again the same day: an initial
single-post-only version turned out too narrow for a genuinely busy
period with several distinct important stories -- forcing everything into
one 260-char synthesis lost real content. Unlike the old batch design,
though, there's still no queue: every accepted post in a call's batch
fires immediately, one after another, in that same run -- nothing spreads
across subsequent hourly runs anymore, and no two posts in the same batch
may cover the same story (enforced both in the prompt and, for the
duplicate-figures case, deterministically -- see run()'s prior_texts).

Because there's no queue, there's nothing to pace across a day/night
window either -- the 3 fixed checkpoints ARE the schedule. The external
cron-job.org dispatch stays exactly as it is (still hourly) --
_ready_for_call is what turns "hourly dispatch" into "only acts 3x/day,"
same mechanism as before, just with 3 checkpoint hours instead of 8.

Primary input is _world_news_snapshot (config/world_news.json's general
outlets -- Guardian, BBC, Deutsche Welle, France 24, Euronews, plus
non-English sources translated inline by Claude itself while writing the
recap: la Repubblica, Corriere della Sera, Le Monde, El Pais, Der
Spiegel). Unlike the keyword-gated crypto/finance feeds, these are pulled
unconditionally (news_rss.fetch_latest_articles, no keyword whitelist --
"what's the latest important news" doesn't fit a keyword filter the way
a finance alert does). Secondary/supporting inputs -- prices, the
CryptoScope Oracle, the keyword-gated crypto/finance/AI news, earnings,
press releases -- are unchanged from the old design but explicitly
deprioritized in the prompt now.

Reply decisions live in their own, much faster cadence in
reply_manager.py. Reposting (retweet/quote-tweet) is a manual, human
decision only -- this trigger never touches X's retweet/quote endpoints.

No images, no links on X, by deliberate account-wide choice -- instead,
every recap's second_part field is mandatory (Claude must always fill it
in, see ai_manager_brain.py's prompt): a reply posted immediately after
the main post whose one job is explaining what it actually means in
clear, simple terms. Carried over unchanged from the old per-story design,
including the anti-leak hardening (_reasoning_contradicts_post also
checks second_part, not just reasoning -- confirmed live that Claude's
own internal second-guessing could otherwise get posted verbatim as a
reply).

Two independent hard budget caps gate this trigger, each stopping it
cleanly rather than erroring when exhausted:
  - ctx.claude_budget (config/claude_budget.json) -- gates whether a new
    recap-generating Claude call is even attempted.
  - ctx.budget (config/budget.json) -- gates whether a decided post/
    second_part is actually sent to X (same shared pool every other
    trigger uses).

Every call sends a short Telegram bot-chat status line (whether a new
call happened and why not if not) plus, on a genuine call, an audit
message with the post text (or decline reasoning) -- the only review
mechanism now that nothing is manually approved.
"""
import logging
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from src import story_history, telegram_client
from src.formatting import fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain, news_rss, twelvedata

CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")

# A dollar figure with a scale word, or a percentage -- distinctive enough
# that two unrelated real stories sharing 2+ of them verbatim is rare.
_SALIENT_NUMBER_RE = re.compile(
    r"\$\s?\d[\d,.]*\s?(?:million|billion|trillion|M|B|K)\b|\d+(?:\.\d+)?%",
    re.IGNORECASE,
)

logger = logging.getLogger("tickerwatch.triggers.ai_manager")


def _roll_day(state, today_str):
    """Rolls over the Claude-call cadence counter -- calendar-day, Brussels
    time (matching _ready_for_call's checkpoint clock), since
    max_calls_per_day is about controlling Claude spend across the 3 fixed
    checkpoints, not a separate posting cadence anymore."""
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0


def _day_context(ctx):
    """A plain-English line telling Claude what day it actually is, so it
    can phrase STOCK price timing correctly -- confirmed live that a post
    said stocks moved 'today' on a weekend, when US markets are closed and
    that move actually happened in Friday's session. Crypto and world news
    trade/happen 24/7 so they never have this problem; this is purely a
    US-stock-market framing issue. Simple weekday/weekend check (via
    ctx.now, not US-Eastern-exact) -- doesn't account for market holidays,
    but that's a much rarer edge case than every single weekend."""
    day_name = ctx.now.strftime("%A, %B %d, %Y")
    if ctx.now.weekday() >= 5:  # Saturday=5, Sunday=6
        return (
            f"{day_name} -- a weekend. US stock markets are closed; any stock price move "
            "reflects the last trading session (Friday), not something that happened today. "
            "Crypto trades 24/7 and world news is unaffected."
        )
    return f"{day_name} -- a weekday. US stock markets are open during their normal trading hours."


# Fixed clock checkpoints (Europe/Brussels): morning, midday, evening --
# three real posting moments a day rather than a high-frequency drip.
# Deliberately clock-time-based, not elapsed-time+jitter -- same lesson
# already learned once this session (an elapsed-time cadence drifted over
# time and produced uneven gaps); a fixed clock is predictable instead.
_CALL_CHECKPOINT_HOURS = (6, 12, 21)


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


def _world_news_snapshot(ctx, state, limit=15):
    """PRIMARY input to the recap (see ai_manager_brain.py's prompt) --
    latest headlines from config/world_news.json's general world-news
    outlets, via news_rss.fetch_latest_articles (no keyword gate -- "what's
    the latest important news" doesn't fit a finance/crypto keyword
    whitelist the way a JUST IN alert does).

    Time-filtered to since the last successful call (state["last_call_time"])
    -- this, not URL exclusion, is the real dedup mechanism here: a recap
    synthesizes many articles into one post with no single source URL to
    log (_post_recap logs url=None), so story_history's usual recent_urls
    exclusion is a no-op for this feed type. Without the time filter, the
    same still-top-of-feed articles could keep reappearing in the candidate
    pool call after call on a quiet news day. First-ever call (no
    last_call_time yet) passes since_ts=None, so nothing gets filtered out
    by time on that bootstrap run. already_posted_urls is still passed too,
    as a cheap secondary guard, same cross-trigger dedup pattern
    _news_snapshot already uses."""
    world_cfg = ctx.config["world_news"]
    already_used = story_history.recent_urls(ctx.state, ctx.now.timestamp())
    since_ts = state.get("last_call_time")
    try:
        articles = news_rss.fetch_latest_articles(
            world_cfg["rss_feeds"], already_used, world_cfg.get("max_articles_per_feed", 3), since_ts=since_ts
        )
    except Exception:
        logger.exception("World news fetch failed for ai_manager")
        return []
    return articles[:limit]


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
    _fetch_oracle_data from live Kraken price history -- see
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
    """Secondary/supporting input now (world news is primary -- see
    _world_news_snapshot): the same keyword-gated crypto/finance/AI feeds
    (config/keywords.json) used by news_alerts.py. Excludes articles
    already covered by ANY trigger within the shared story_history
    window."""
    kw_cfg = ctx.config["keywords"]
    already_used = story_history.recent_urls(ctx.state, ctx.now.timestamp())
    try:
        return news_rss.fetch_matching_articles(kw_cfg["rss_feeds"], kw_cfg["keywords"], already_used, limit)
    except Exception:
        logger.exception("News fetch failed for ai_manager")
        return []


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
    recapped twice -- confirmed live that Claude can independently
    regenerate a post covering a topic already sitting right there in its
    own RECENTLY POSTED context, despite the explicit prompt rule against
    it ("should_post: false since this was already covered" is a request
    to Claude, not a guarantee -- same reasoning as every other code-level
    backstop in this module: cashtag, opening tag). Flags a likely
    duplicate when a candidate shares min_shared+ distinctive figures (a
    dollar amount with a scale word, or a percentage) verbatim with any
    already-posted text -- two unrelated real stories coincidentally
    sharing two exact figures is rare enough that this stays low on false
    positives while catching the actual observed failure (the same
    "$400 million... $20 billion" Citadel/Crypto.com story posted twice)."""
    candidate_nums = _salient_numbers(text)
    if len(candidate_nums) < min_shared:
        return False
    for prior in prior_texts:
        if len(candidate_nums & _salient_numbers(prior)) >= min_shared:
            return True
    return False


# Confirmed live, verbatim, multiple times now, with different exact
# phrasing each time -- a decision's reasoning field correctly diagnosed
# the problem and then contradicted itself in the very next field
# (should_post stayed true), and once even leaked straight into the
# published second_part reply itself ("Wait -- this was already covered.
# Skipping to avoid repeat." -- a real posted reply, not just a reasoning
# field). When either reasoning or second_part says this plainly, trust it
# over the boolean.
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


def _reasoning_contradicts_post(text):
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _NEGATIVE_REASONING_PHRASES)


def _enforce_opening_tag(text):
    """Every recap must open with ai_manager_brain.TAG -- same
    defense-in-depth pattern as _enforce_single_cashtag: the prompt already
    requires this, but a rule stated in a prompt is a request, not a
    guarantee, and a post silently missing its tag breaks the profile's
    visual consistency. Prepends the tag if it's somehow missing rather
    than letting the post go out untagged."""
    stripped = text.lstrip()
    tag_prefix = f"{ai_manager_brain.TAG}:"
    if stripped.startswith(tag_prefix):
        return text
    return f"{tag_prefix} {text}"


def _minutes_to_next_checkpoint(ctx):
    """Exact minutes until the next fixed checkpoint (see
    _CALL_CHECKPOINT_HOURS) -- clock times are deterministic, so this is an
    exact countdown, not an estimate."""
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


def _send_run_summary(ctx, reason, posted_this_run):
    """One short bot-chat-only line every run (never the public channel),
    regardless of what happened -- lets you tell at a glance whether this
    run made a new Claude call or not (and why not) and whether it posted.
    When the reason is the cooldown gate specifically, also shows the exact
    time to the next checkpoint."""
    post_label = "posted" if posted_this_run else "no post"
    eta_suffix = ""
    if "cooldown" in reason:
        h, m = divmod(_minutes_to_next_checkpoint(ctx), 60)
        eta_suffix = f" · next call in ~{h}h {m}m" if h else f" · next call in ~{m}m"
    telegram_client.send_message(f"🤖 AI Manager: {reason} · {post_label}{eta_suffix}")


def _send_audit_message(posted_items, declined_items):
    """Fires once per genuine Claude call -- the only review mechanism now
    that nothing is manually approved. Lists every post that actually went
    out (with its reasoning) and every candidate declined, with why --
    either Claude's own reasoning (never published) or a deterministic
    backstop's reason."""
    lines = ["🤖 AI Manager batch decision:"]
    if posted_items:
        lines.append(f"\n✅ {len(posted_items)} post(s) published:")
        for item in posted_items:
            lines.append(f"\n- {item['text']}\nReasoning: {item['reasoning'] or '(none given)'}")
    else:
        lines.append("\n✅ 0 posts published.")
    if declined_items:
        lines.append(f"\n🚫 {len(declined_items)} candidate(s) declined:")
        for item in declined_items:
            lines.append(f"\n- [{item['reason']}] {item['reasoning'] or '(none given)'}")
    telegram_client.send_message("\n".join(lines))


def _post_one(ctx, item, prior_texts):
    """Validates and fires (or declines) a single candidate post from the
    batch. Returns (fired: bool, detail: str) -- on success detail is the
    full published text (post + second_part, for the audit log); on decline
    it's a short machine-readable reason. Every check here is a
    deterministic backstop on top of what the prompt already asks for (see
    _reasoning_contradicts_post/_is_likely_duplicate's own docstrings for
    why a prompt rule alone isn't trusted blindly). prior_texts covers both
    this account's real recent post history AND every post already accepted
    earlier in this same batch (the caller extends it after each accept),
    so within-batch duplicates get caught the same way cross-call ones do."""
    text = item.get("text")
    if not text:
        return False, "empty text"

    second_part = item.get("second_part") or ""
    if not second_part:
        return False, "missing mandatory second_part"

    # Only second_part is scanned here, not reasoning -- confirmed live
    # (2026-07-20) that scanning reasoning produces false positives: Claude
    # can legitimately narrate its whole batch's selection process in
    # reasoning ("the Fed story was already covered, so I focused on the UK
    # PM transition instead"), which is never published (see
    # ai_manager_brain.py's prompt) and isn't a red flag about the post it
    # actually chose. second_part has no such ambiguity -- it's published
    # content with exactly one job (explain this post), so any of these
    # phrases there is a genuine red flag -- this is the same check that
    # caught the real leaked-self-doubt bug.
    if _reasoning_contradicts_post(second_part):
        logger.warning(
            "ai_manager: declining post whose second_part contradicts itself: %s", second_part[:120]
        )
        return False, "second_part contradicts itself"

    if _is_likely_duplicate(text, prior_texts) or _is_likely_duplicate(second_part, prior_texts):
        logger.warning(
            "ai_manager: declining likely-duplicate post (shared salient figures with a recent/batch post): %s",
            text[:80],
        )
        return False, "likely duplicate"

    if not ctx.budget.can_spend(has_link=False):
        return False, "X budget exhausted this period"

    tagged_text = _enforce_opening_tag(text)
    final_text = _enforce_single_cashtag(truncate(tagged_text, ai_manager_brain.MAX_POST_LEN))

    tweet_id = ctx.x.post(final_text)
    if not tweet_id:
        # ctx.x.post() itself failed -- ops_alerts already fired for this
        telegram_client.send_message(f"⚠️ AI Manager: post failed to send, dropped: {final_text}")
        return False, "X post failed"

    channel_text = f"{tagged_text}\n\n{second_part}"
    ctx.budget.record_spend(has_link=False, text=final_text, channel_text=channel_text)
    if ctx.budget.can_spend(has_link=False):
        reply_text = _enforce_single_cashtag(truncate(second_part, ai_manager_brain.MAX_POST_LEN))
        reply_id = ctx.x.reply(reply_text, tweet_id)
        if reply_id:
            # already mirrored to the channel above via channel_text, skip duplicate
            ctx.budget.record_spend(has_link=False, text=second_part, mirror_to_channel=False)

    story_history.add_entry(ctx.state, text=final_text, url=None, now_ts=ctx.now.timestamp())
    return True, channel_text


def run(ctx):
    cfg = ctx.config["ai_manager"]
    state = ctx.state["ai_manager"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    _roll_day(state, today_str)
    state.setdefault("last_call_checkpoint", None)

    if not _ready_for_call(ctx, cfg, state):
        _send_run_summary(ctx, "no new call (cooldown/daily cap)", False)
        return False
    if not ctx.claude_budget.can_spend():
        logger.info("ai_manager: Claude budget exhausted this month, skipping call")
        _send_run_summary(ctx, "no new call (Claude budget capped)", False)
        return False

    max_posts_per_call = cfg.get("max_posts_per_call", 4)
    snapshot = {
        "day_context": _day_context(ctx),
        "world_news": _world_news_snapshot(ctx, state),
        "news": _news_snapshot(ctx),
        "prices": _price_snapshot_lines(ctx),
        "oracle": _oracle_snapshot_lines(ctx),
        "earnings": _earnings_snapshot(ctx),
        "press_releases": _press_releases_snapshot(ctx),
        "own_recent_posts": story_history.recent_texts(ctx.state, ctx.now.timestamp()),
        "max_posts_per_call": max_posts_per_call,
    }

    decision, usage = ai_manager_brain.decide(snapshot, cfg["model"])
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        # outright API failure or an unparseable response -- don't mark this
        # checkpoint as used on a call that produced nothing usable. The
        # earliest retry is still the next checkpoint (not sooner --
        # _ready_for_call requires an aligned checkpoint hour regardless),
        # but at least a persistently broken call doesn't permanently burn
        # today's checkpoint slot. calls_today still increments either way,
        # so repeated failures can't retry more than max_calls_per_day times.
        _send_run_summary(ctx, "new call failed/unparsed, will retry next checkpoint", False)
        return False

    # only a successfully parsed decision marks this checkpoint as used
    state["last_call_time"] = ctx.now.timestamp()
    state["last_call_checkpoint"] = ctx.now.astimezone(BRUSSELS_TZ).strftime("%Y-%m-%d-%H")

    # Full uncapped window, not the prompt's own capped own_recent_posts --
    # cheap local comparison, not prompt tokens, so it can afford to check
    # everything rather than only what fit in the snapshot. Extended below
    # after each accepted post, so a later item in this same batch can't
    # repeat an earlier one in it either.
    prior_texts = story_history.recent_texts(ctx.state, ctx.now.timestamp(), limit=None)
    posted_items = []
    declined_items = []

    for item in (decision.get("posts") or [])[:max_posts_per_call]:
        reasoning = item.get("reasoning", "")
        fired_one, detail = _post_one(ctx, item, prior_texts)
        if fired_one:
            posted_items.append({"text": detail, "reasoning": reasoning})
            prior_texts = prior_texts + [item.get("text", ""), item.get("second_part") or ""]
        else:
            declined_items.append({"reason": detail, "reasoning": reasoning})

    fired = bool(posted_items)
    _send_audit_message(posted_items, declined_items)
    _send_run_summary(ctx, f"new call ({len(posted_items)} posted, {len(declined_items)} declined)", fired)
    return fired
