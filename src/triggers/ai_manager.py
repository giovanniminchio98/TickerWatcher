"""Post type 11 (opt-in via ANTHROPIC_API_KEY presence): a 4x/day
(02:00/06:00/12:00/21:00 Brussels -- see _CALL_CHECKPOINT_HOURS) financial-
intelligence feed. Each call is handed a large pool of candidate crypto/
finance/AI articles (config/keywords.json's feeds -- see
_candidate_news_snapshot), and Claude SCORES and FILTERS them, rather than
just picking a headline to paraphrase. See src/sources/ai_manager_brain.py
for the full rubric/prompt.

Redesigned entirely (2026-07-23) from the previous "3-4x/day world-news
recap" design: the account owner's read was that engagement was weak, the
account read as posting routine "fuzz" rather than content worth reading,
and asked for "no war, but more finance and useful insight" -- a real
quality filter instead of "did an article match a keyword." World news
(config/world_news.json) is dropped entirely as an input; crypto/finance/AI
news (config/keywords.json) becomes the primary and only news lens, at a
much larger pool size than before (candidate_pool_size, default 80, vs. the
old secondary snapshot's limit=6) so there's actually something to filter
down from.

Two tiers come out of one call:
  - Individual posts (score >= individual_post_min_score, default 75): a
    full "market intelligence card" -- main tweet (category emoji + hook +
    a pointer to the reply) and a structured reply (why it matters,
    bullish/bearish tickers, impact score, confidence, time horizon, bottom
    line), assembled deterministically in code from Claude's structured
    fields (see _assemble_main_tweet/_assemble_reply_card) -- never trusted
    as raw prose, same "a prompt rule is a request, not a guarantee"
    philosophy as every other backstop in this module (cashtag
    enforcement, category tag enforcement, duplicate detection). A crypto
    story whose chart_symbol names a tracked coin gets a real price chart
    attached (see src/sources/chart_gen.py) -- the account's first
    data-driven image, as opposed to media.py/oracle_media.py's static
    generic assets.
  - A digest thread (score band digest_min_score..individual_post_min_score,
    only if at least digest_min_items qualify after code-side dedup/
    validation): secondary stories that don't individually clear the full
    bar, bundled into one numbered reply-thread instead of getting their
    own mediocre post or being dropped outright. x_client.py has no native
    thread helper, so _post_digest_thread chains ctx.x.reply() calls
    manually, each replying to the previous tweet's own returned ID.

Every candidate is referenced by its INDEX into the snapshot's candidate
list (source_index), never by Claude copying back title/URL as free text --
an LLM asked to echo a string verbatim can still alter it, which would
silently break both dedup and the real article URL story_history needs.

Duplicate detection got a second, structurally new layer this redesign:
alongside the existing _is_likely_duplicate (shared salient dollar-figures/
percentages), _is_same_story_title does token-overlap comparison against
recent posts' own SOURCE ARTICLE TITLES -- something only possible now that
every selected item traces to one real candidate article (the old
world-news recap had no single source title to compare against, url=None
always). This directly targets the exact failure that got the old design
paused: two personnel/political stories, worded completely differently,
with no shared number to catch on the old check alone. Applied twice: as a
pre-filter before candidates ever reach Claude (_candidate_news_snapshot),
and as a deterministic backstop after Claude's own selection
(_post_individual_item/_prepare_digest_items) -- same dual-layer pattern as
every other guard in this module.

The 02:00 checkpoint, the 4x/day cadence, the checkpoint-hour gate
mechanism, and the two independent budget gates (ctx.claude_budget before
the call, ctx.budget before posting) all carry over unchanged from the
previous design -- see _ready_for_call/_CALL_CHECKPOINT_HOURS. The external
cron-job.org dispatch stays exactly as it is (still hourly); this trigger's
own checkpoint gate is what turns "hourly dispatch" into "only acts a few
times a day."

Reply decisions live in their own, much faster cadence in reply_manager.py.
Reposting (retweet/quote-tweet) is a manual, human decision only -- this
trigger never touches X's retweet/quote endpoints.

Every call sends a short Telegram bot-chat status line (whether a new call
happened and why not if not) plus, on a genuine call, an audit message with
every post/decline/digest decision -- the only review mechanism now that
nothing is manually approved.
"""
import logging
import random
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from src import story_history, telegram_client
from src.formatting import fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain, chart_gen, news_rss, twelvedata

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
    max_calls_per_day is about controlling Claude spend across the 4 fixed
    checkpoints, not a separate posting cadence."""
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0
        state["checkpoint_attempts"] = {}


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


# Fixed clock checkpoints (Europe/Brussels): morning, midday, evening --
# real posting moments a day rather than a high-frequency drip. Deliberately
# clock-time-based, not elapsed-time+jitter -- an elapsed-time cadence
# drifted over time and produced uneven gaps; a fixed clock is predictable.
_CALL_CHECKPOINT_HOURS = (2, 6, 12, 21)


def _current_checkpoint_id(brussels_now):
    """Which checkpoint 'slot' owns the current hour -- the most recent
    checkpoint hour at or before now (wrapping to yesterday's last
    checkpoint before today's first one). A slot spans from its checkpoint
    hour up to (not including) the next checkpoint hour, so every hourly
    dispatch in between maps to the same slot -- this is what lets a
    failed call retry on the very next hourly run instead of only at the
    next fixed checkpoint (2026-07-23: confirmed live that a noon call
    failing on an empty/unparseable Claude response otherwise left a
    ~9-hour hole with no retry until 21:00)."""
    hour = brussels_now.hour
    if hour < _CALL_CHECKPOINT_HOURS[0]:
        owning_hour = _CALL_CHECKPOINT_HOURS[-1]
        date_str = (brussels_now - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        owning_hour = max(h for h in _CALL_CHECKPOINT_HOURS if h <= hour)
        date_str = brussels_now.strftime("%Y-%m-%d")
    return f"{date_str}-{owning_hour:02d}"


def _ready_for_call(ctx, cfg, state):
    """A call is only ever first attempted right at a fixed checkpoint
    hour, same as before -- but if that attempt (or a subsequent retry)
    fails, this allows retrying again on every following hourly dispatch
    within the same slot (up to max_attempts_per_checkpoint total tries)
    instead of waiting for the next fixed checkpoint. The distinction is
    exactly "has a prior attempt already failed for this slot": zero
    attempts + not on the checkpoint hour itself -> wait; one or more
    failed attempts -> retry on any hour until success or the attempt cap
    is hit. The real backstop against runaway retry cost is still
    ctx.claude_budget's absolute monthly cap (checked separately by the
    caller) plus max_calls_per_day here -- this is a pacing knob, not the
    cost control."""
    if state["calls_today"] >= cfg["max_calls_per_day"]:
        return False
    brussels_now = ctx.now.astimezone(BRUSSELS_TZ)
    checkpoint_id = _current_checkpoint_id(brussels_now)
    if state.get("last_call_checkpoint") == checkpoint_id:
        return False  # this slot already has a successful call
    attempts = state.get("checkpoint_attempts", {}).get(checkpoint_id, 0)
    at_checkpoint_hour = brussels_now.hour in _CALL_CHECKPOINT_HOURS
    if not at_checkpoint_hour and attempts == 0:
        return False  # no failed attempt yet this slot -- wait for its own checkpoint hour
    return attempts < cfg.get("max_attempts_per_checkpoint", 4)


# Stopwords stripped before token-overlap comparison in _is_same_story_title
# -- common words that would otherwise inflate apparent overlap between two
# genuinely unrelated headlines that just happen to share ordinary English.
_TITLE_STOPWORDS = frozenset(
    """the a an of in on for to and or is are with at by its as after over amid says said new will
    has have had was were be been from that this than into out up down more most not but so it he
    she they his her their who what when where why how can could would should may might also still
    just now than about all one two three""".split()
)


def _title_tokens(title):
    words = re.findall(r"[A-Za-z0-9']+", (title or "").lower())
    return {w for w in words if w not in _TITLE_STOPWORDS and len(w) > 2}


def _is_same_story_title(candidate_title, prior_titles, min_shared_tokens=3, min_overlap_ratio=0.5):
    """Flags likely-same-real-world-event coverage even with zero shared
    numbers, by token overlap between the candidate's own source article
    title and recent posts' source titles -- catches the exact class of
    failure the old numeric-only check structurally couldn't (personnel/
    political stories, worded completely differently, with no shared dollar
    figure or percentage). min_overlap_ratio=0.5 (not a stricter 0.6): a
    real reworded repeat of the same event still loses some overlap to
    plain word-form drift (e.g. "mounting" vs "mounts") without actually
    being a different story, so the bar is "at least half the smaller
    title's meaningful words," not "nearly all of them." Deliberately a
    starting heuristic to tune after live observation, same as every other
    threshold in this module."""
    cand_tokens = _title_tokens(candidate_title)
    if len(cand_tokens) < min_shared_tokens:
        return False
    for prior in prior_titles:
        prior_tokens = _title_tokens(prior)
        if not prior_tokens:
            continue
        shared = cand_tokens & prior_tokens
        if len(shared) < min_shared_tokens:
            continue
        if len(shared) / min(len(cand_tokens), len(prior_tokens)) >= min_overlap_ratio:
            return True
    return False


def _candidate_news_snapshot(ctx, cfg):
    """PRIMARY (and only) news input now -- a much larger pool than the old
    secondary snapshot (candidate_pool_size, default 80, vs. the old
    limit=6), pulled from config/keywords.json's feeds via the same
    news_rss.fetch_matching_articles used by news_alerts.py. Pre-filtered
    by _is_same_story_title against story_history's recent source titles
    before candidates ever reach the prompt -- saves tokens and removes
    reliance on Claude's own judgment for the common case; the same check
    runs again as a deterministic backstop after selection (see
    _post_individual_item/_prepare_digest_items)."""
    kw_cfg = ctx.config["keywords"]
    pool_size = cfg.get("candidate_pool_size", 80)
    max_per_feed = cfg.get("candidate_pool_max_per_feed", 8)
    already_used = story_history.recent_urls(ctx.state, ctx.now.timestamp())
    try:
        articles = news_rss.fetch_matching_articles(
            kw_cfg["rss_feeds"], kw_cfg["keywords"], already_used, pool_size, max_per_feed=max_per_feed
        )
    except Exception:
        logger.exception("Candidate news fetch failed for ai_manager")
        return []
    recent_titles = story_history.recent_source_titles(ctx.state, ctx.now.timestamp())
    return [a for a in articles if not _is_same_story_title(a["title"], recent_titles)]


def _price_snapshot_lines(ctx):
    """Crypto comes from ctx.prices (already fetched once per run by
    main.py). Stocks use watchlist.stocks_broad (30 tickers) via
    twelvedata.get_quotes_batch -- chunked into 5-symbol requests with a
    full 60s pause between chunks (confirmed live that a shorter 15s pause
    still hit Twelve Data's free-tier per-minute limit). Adds ~5 minutes to
    a call that needs it, deliberately accepted to get real stock data
    reliably rather than dropping the feature. Never raises, so no
    try/except needed here -- a symbol just won't have a line if its chunk
    didn't come through. Falls back to the smaller 'stocks' list if
    stocks_broad isn't configured."""
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
    (analyze() returned None) are simply omitted rather than padded with a
    placeholder line."""
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
    try/except-and-default-to-empty pattern as every other external call in
    this module."""
    try:
        entries = twelvedata.get_earnings_calendar()
    except Exception:
        logger.exception("Twelve Data earnings_calendar fetch failed for ai_manager")
        return []
    tracked = {asset["symbol"] for asset in ctx.config["watchlist"].get("stocks_broad", [])}
    return [e for e in entries if e.get("symbol") in tracked]


def _press_releases_snapshot(ctx, max_results=10):
    """Recent official press releases (Twelve Data, free-tier endpoint) for
    watchlist.stocks_broad -- a primary-source angle distinct from the
    RSS/journalism news already used elsewhere. Same
    try/except-and-default-to-empty pattern as everything else here."""
    symbols = [asset["symbol"] for asset in ctx.config["watchlist"].get("stocks_broad", [])]
    try:
        return twelvedata.get_press_releases(symbols, max_results=max_results)
    except Exception:
        logger.exception("Twelve Data press_releases fetch failed for ai_manager")
        return []


def _enforce_single_cashtag(text):
    """X hard-rejects (403 Forbidden) any single post with more than one
    $cashtag -- confirmed live (a post naming both $STRF and $STRC failed
    to send entirely). Keeps the first cashtag intact (genuinely nice to
    have: free, and X renders it with a live price card) and strips just
    the leading '$' from any additional ones, so the post still reads
    naturally instead of failing to send at all. Also what makes it safe
    for _assemble_reply_card to freely list several bullish/bearish
    tickers as $cashtags -- this enforcement demotes all but the first to
    plain text before the card ever reaches ctx.x.post/reply."""
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
    ALREADY COVERED context, despite the explicit prompt rule against it
    (a prompt rule is a request, not a guarantee -- same reasoning as every
    other code-level backstop in this module: cashtag, category tag,
    _is_same_story_title). Flags a likely duplicate when a candidate shares
    min_shared+ distinctive figures (a dollar amount with a scale word, or a
    percentage) verbatim with any already-posted text -- two unrelated real
    stories coincidentally sharing two exact figures is rare enough that
    this stays low on false positives while catching the actual observed
    failure (the same "$400 million... $20 billion" Citadel/Crypto.com
    story posted twice)."""
    candidate_nums = _salient_numbers(text)
    if len(candidate_nums) < min_shared:
        return False
    for prior in prior_texts:
        if len(candidate_nums & _salient_numbers(prior)) >= min_shared:
            return True
    return False


# Confirmed live, verbatim, multiple times, with different exact phrasing
# each time -- a decision's reasoning field correctly diagnosed the problem
# and then contradicted itself in the very next field, and once leaked
# straight into a published reply. When published content says this
# plainly, trust it over the score/tier.
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


def _enforce_category_tag(text, category):
    """Every main tweet must open with its category's emoji directly on the
    same line as the first word -- same defense-in-depth pattern as
    _enforce_single_cashtag: the prompt already asks for a category, but a
    rule stated in a prompt is a request, not a guarantee. An unrecognized
    or missing category falls back to Macro's emoji rather than posting
    untagged."""
    emoji = ai_manager_brain.CATEGORY_EMOJI.get(category, ai_manager_brain.CATEGORY_EMOJI["Macro"])
    stripped = text.lstrip()
    if stripped.startswith(f"{emoji} "):
        return text
    return f"{emoji} {stripped}"


# Varied pointer from the main tweet to its reply card -- same "randomly
# chosen so it doesn't read as the same fixed line every time" reasoning as
# news_alerts.py's own _REPLY_POINTERS, adapted to this trigger's
# hook+card shape instead of a headline+explanation shape.
_REPLY_POINTERS = (
    "Why it matters below",
    "Here's the context",
    "The full picture",
    "Breaking it down below",
    "Full breakdown below",
)


def _assemble_main_tweet(hook, category):
    tagged = _enforce_category_tag(hook, category)
    pointer = random.choice(_REPLY_POINTERS)
    return f"{tagged}\n\n📌 {pointer} 👇"


def _assemble_reply_card(item):
    """Builds the reply card greedily within budget IN CODE, not the
    prompt -- confirmed live (2026-07-23) that per-field character caps
    stated in the prompt don't reliably keep the ASSEMBLED total under X's
    real tweet limit: a card with 3 bullets + tickers + impact/confidence/
    horizon + a bottom-line closer regularly overflowed 260 chars, and
    truncate()'s last-resort ellipsis fallback (no paragraph break to fall
    back to in this bullet-list shape) silently chopped it mid-sentence,
    dropping the mandatory bottom line entirely -- the exact class of bug
    already fixed once for the old design's chunk-A/chunk-B post shape.
    bottom_line is built in first and never dropped; bullets/tickers/stats
    are added only while there's still room, in priority order, so a card
    that runs long degrades by quietly dropping its least essential lines
    instead of getting chopped mid-thought. Since Claude writes bullets in
    the order it thinks matters most (see the prompt), the first one is
    the one most likely to survive."""
    bottom_line = (item.get("bottom_line") or "").strip()
    bottom = f"🎯 {bottom_line}"

    candidates = [f"• {b}" for b in (item.get("why_it_matters") or [])[:3]]

    tickers = [f"🐂${t}" for t in (item.get("tickers_bullish") or [])[:2]]
    tickers += [f"🐻${t}" for t in (item.get("tickers_bearish") or [])[:2]]
    if tickers:
        candidates.append(" ".join(tickers))

    stats = []
    if item.get("impact_score") is not None:
        stats.append(f"📊{item['impact_score']}/10")
    if item.get("confidence"):
        stats.append(f"🔍{item['confidence']}")
    if item.get("time_horizon"):
        stats.append(f"⏳{item['time_horizon']}")
    if stats:
        candidates.append(" · ".join(stats))

    budget = ai_manager_brain.MAX_POST_LEN
    lines = []
    used = len(bottom)
    for line in candidates:
        added = len(line) + 1  # +1 for its own newline
        if used + added > budget:
            continue
        lines.append(line)
        used += added
    lines.append(bottom)
    return "\n".join(lines)


def _assemble_digest_tweet(item, idx, total):
    """Same "never silently chop the load-bearing part" principle as
    _assemble_reply_card: the numbered headline is what makes this line
    legible as part of a thread, so it's kept whole except as an absolute
    last resort. The "why it matters" clause and ticker suffix degrade
    first if there's no room -- dropped, then truncated at a sentence
    boundary, rather than the whole line getting an ugly mid-word
    ellipsis cut (confirmed live this shape can overflow 260 chars too,
    same root cause as the reply card)."""
    emoji = ai_manager_brain.CATEGORY_EMOJI.get(item.get("category"), ai_manager_brain.CATEGORY_EMOJI["Macro"])
    headline = (item.get("headline") or "").strip()
    why = (item.get("why_it_matters") or "").strip()
    tickers = item.get("tickers") or []
    ticker_suffix = (" " + " ".join(f"${t}" for t in tickers[:2])) if tickers else ""

    prefix = f"{emoji} {idx}/{total}: {headline}"
    budget = ai_manager_brain.MAX_POST_LEN
    if len(prefix) >= budget:
        return prefix[: budget - 1].rstrip() + "…"

    remaining = budget - len(prefix) - 1  # -1 for the newline before line 2
    second_line = f"{why}{ticker_suffix}"
    if len(second_line) > remaining:
        second_line = truncate(second_line, remaining) if remaining >= 20 else ""
    if not second_line:
        return prefix
    return f"{prefix}\n{second_line}"


def _resolve_candidate(candidates, source_index):
    if isinstance(source_index, int) and 0 <= source_index < len(candidates):
        return candidates[source_index]
    return None


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


def _send_audit_message(posted_items, declined_items, digest_fired, digest_texts):
    """Fires once per genuine Claude call -- the only review mechanism now
    that nothing is manually approved. Lists every individual post that
    went out (with its reasoning), every candidate declined and why, and
    the digest thread's outcome."""
    lines = ["🤖 AI Manager batch decision:"]
    if posted_items:
        lines.append(f"\n✅ {len(posted_items)} individual post(s) published:")
        for item in posted_items:
            lines.append(f"\n- {item['text']}\nReasoning: {item['reasoning'] or '(none given)'}")
    else:
        lines.append("\n✅ 0 individual posts published.")
    if declined_items:
        lines.append(f"\n🚫 {len(declined_items)} candidate(s) declined:")
        for item in declined_items:
            lines.append(f"\n- [{item['reason']}] {item['reasoning'] or '(none given)'}")
    if digest_fired:
        lines.append(f"\n🧵 Digest thread posted ({len(digest_texts)} tweet(s)):")
        for t in digest_texts:
            lines.append(f"\n- {t}")
    else:
        lines.append("\n🧵 No digest thread this run.")
    telegram_client.send_message("\n".join(lines))


def _post_individual_item(ctx, item, candidates, prior_texts, prior_titles, cfg, tracked_crypto_symbols):
    """Validates and fires (or declines) one Claude-selected candidate as a
    full individual post. Returns (fired: bool, detail: str, candidate:
    dict|None) -- on success detail is the full published text (main tweet
    + reply card, for the audit log) and candidate is the source article
    (so the caller can extend prior_titles); on decline detail is a short
    machine-readable reason and candidate is None. Every check here is a
    deterministic backstop on top of what the prompt already asks for."""
    score = item.get("score")
    if not isinstance(score, (int, float)) or score < cfg.get("individual_post_min_score", 75):
        return False, "score below individual-post bar", None

    candidate = _resolve_candidate(candidates, item.get("source_index"))
    if candidate is None:
        return False, "invalid source_index", None

    hook = (item.get("hook") or "").strip()
    if not hook:
        return False, "empty hook", None

    bottom_line = (item.get("bottom_line") or "").strip()
    if not bottom_line:
        return False, "missing mandatory bottom_line", None

    if _is_same_story_title(candidate["title"], prior_titles):
        return False, "likely same story (title overlap)", None
    if _is_likely_duplicate(hook, prior_texts) or _is_likely_duplicate(bottom_line, prior_texts):
        return False, "likely duplicate (shared salient figures)", None

    reply_card = _assemble_reply_card(item)
    if _reasoning_contradicts_post(reply_card):
        logger.warning("ai_manager: declining post whose reply card contradicts itself: %s", reply_card[:120])
        return False, "reply card contradicts itself", None

    if not ctx.budget.can_spend(has_link=False):
        return False, "X budget exhausted this period", None

    main_text_raw = _assemble_main_tweet(hook, item.get("category"))
    final_main = _enforce_single_cashtag(truncate(main_text_raw, ai_manager_brain.MAX_POST_LEN))
    # Same "decline rather than post broken" rule as the old design's
    # chunk-A/chunk-B check -- the pointer line is meaningless without a
    # reply behind it, so a truncation that drops it produces a broken
    # post, not a shorter-but-complete one.
    if len(main_text_raw) > ai_manager_brain.MAX_POST_LEN and "\n\n" not in final_main:
        logger.warning(
            "ai_manager: declining post -- main tweet %d chars (limit %d), truncation would drop "
            "the pointer to its reply card: %s",
            len(main_text_raw), ai_manager_brain.MAX_POST_LEN, main_text_raw[:80],
        )
        return False, "text over budget, truncation would drop mandatory content", None

    final_card = _enforce_single_cashtag(truncate(reply_card, ai_manager_brain.MAX_POST_LEN))

    media_id = None
    chart_symbol = item.get("chart_symbol")
    if chart_symbol in tracked_crypto_symbols:
        png_bytes = chart_gen.generate_chart_for_symbol(ctx, chart_symbol)
        if png_bytes:
            media_id = ctx.x.upload_media(png_bytes)

    tweet_id = ctx.x.post(final_main, media_id=media_id)
    if not tweet_id:
        # ctx.x.post() itself failed -- ops_alerts already fired for this
        telegram_client.send_message(f"⚠️ AI Manager: post failed to send, dropped: {final_main}")
        return False, "X post failed", None

    channel_text = f"{final_main}\n\n{final_card}"
    ctx.budget.record_spend(has_link=False, text=final_main, channel_text=channel_text)
    if ctx.budget.can_spend(has_link=False):
        reply_id = ctx.x.reply(final_card, tweet_id)
        if reply_id:
            # already mirrored to the channel above via channel_text, skip duplicate
            ctx.budget.record_spend(has_link=False, text=final_card, mirror_to_channel=False)

    story_history.add_entry(
        ctx.state, text=final_main, url=candidate.get("url"), now_ts=ctx.now.timestamp(),
        source_title=candidate.get("title"),
    )
    return True, channel_text, candidate


def _prepare_digest_items(ctx, cfg, digest, candidates, prior_texts, prior_titles):
    """Validates/dedupes Claude's raw digest.items into a final, sorted
    list ready to post -- ignores digest["should_post"] entirely and
    decides purely from the surviving count after dedup/validation (a code
    backstop over trusting the model's own tier judgment, same philosophy
    as the individual-post score gate). Extends its own local prior_titles
    as it goes so two digest items covering the same event can't both
    survive within the same batch."""
    digest_max_items = cfg.get("digest_max_items", 8)
    digest_min_score = cfg.get("digest_min_score", 45)
    seen_titles = list(prior_titles)
    prepared = []
    for raw in (digest.get("items") or [])[: digest_max_items * 2]:
        score = raw.get("score")
        if not isinstance(score, (int, float)) or score < digest_min_score:
            continue
        candidate = _resolve_candidate(candidates, raw.get("source_index"))
        if candidate is None:
            continue
        headline = (raw.get("headline") or "").strip()
        why = (raw.get("why_it_matters") or "").strip()
        if not headline or not why:
            continue
        if _is_same_story_title(candidate["title"], seen_titles):
            continue
        if _is_likely_duplicate(f"{headline} {why}", prior_texts):
            continue
        prepared.append({"raw": raw, "candidate": candidate, "score": score})
        seen_titles.append(candidate["title"])
    prepared.sort(key=lambda x: x["score"], reverse=True)
    return prepared[:digest_max_items]


_THREAD_MARKER = " 🧵👇"


def _enforce_thread_marker(text):
    """A digest intro read alone in a feed (not clicked into) gives no
    visual cue that a reply thread follows -- confirmed live, a real intro
    ("Today's secondary movers across AI, crypto and markets:") read as a
    complete, standalone tweet with nothing signaling "see replies."
    Appends a thread marker if the text doesn't already end with one --
    same "a prompt rule is a request, not a guarantee" backstop pattern as
    every other enforcement in this module. Truncates first and reserves
    room for the marker so it always survives, rather than risking
    truncate()'s own fallback eating it."""
    stripped = text.rstrip()
    if stripped.endswith(("🧵", "👇")):
        return stripped
    budget = ai_manager_brain.MAX_POST_LEN - len(_THREAD_MARKER)
    return truncate(stripped, budget).rstrip() + _THREAD_MARKER


def _post_digest_thread(ctx, items, digest_intro):
    """Posts an intro tweet, then one numbered reply per item, each
    replying to the previous tweet's own returned ID -- x_client.py has no
    native thread helper, X has no atomic multi-tweet publish endpoint
    either, so this chains ctx.x.reply() calls by hand. Stops early
    (keeping whatever posted so far) the moment budget runs out or a post
    fails -- a partial thread is an accepted degraded outcome, not an
    error, since there's nothing to roll back to anyway. Returns (fired:
    bool, posted_texts: list[str])."""
    if not items or not ctx.budget.can_spend(has_link=False):
        return False, []

    intro_text = (digest_intro or "").strip() or f"{len(items)} more stories worth knowing about today"
    intro_text = _enforce_thread_marker(intro_text)
    final_intro = _enforce_single_cashtag(truncate(intro_text, ai_manager_brain.MAX_POST_LEN))
    tweet_id = ctx.x.post(final_intro)
    if not tweet_id:
        telegram_client.send_message(f"⚠️ AI Manager: digest intro failed to send, dropped: {final_intro}")
        return False, []
    ctx.budget.record_spend(has_link=False, text=final_intro, channel_text=final_intro)

    posted_texts = [final_intro]
    last_id = tweet_id
    total = len(items)
    for i, entry in enumerate(items, start=1):
        if not ctx.budget.can_spend(has_link=False):
            break
        candidate = entry["candidate"]
        text = _assemble_digest_tweet(entry["raw"], i, total)
        final_text = _enforce_single_cashtag(truncate(text, ai_manager_brain.MAX_POST_LEN))
        reply_id = ctx.x.reply(final_text, last_id)
        if not reply_id:
            break
        ctx.budget.record_spend(has_link=False, text=final_text, channel_text=final_text)
        story_history.add_entry(
            ctx.state, text=final_text, url=candidate.get("url"), now_ts=ctx.now.timestamp(),
            source_title=candidate.get("title"),
        )
        posted_texts.append(final_text)
        last_id = reply_id

    return True, posted_texts


def run(ctx):
    cfg = ctx.config["ai_manager"]
    state = ctx.state["ai_manager"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    _roll_day(state, today_str)
    state.setdefault("last_call_checkpoint", None)
    state.setdefault("checkpoint_attempts", {})

    if not _ready_for_call(ctx, cfg, state):
        _send_run_summary(ctx, "no new call (cooldown/daily cap)", False)
        return False
    if not ctx.claude_budget.can_spend():
        logger.info("ai_manager: Claude budget exhausted this month, skipping call")
        _send_run_summary(ctx, "no new call (Claude budget capped)", False)
        return False

    candidates = _candidate_news_snapshot(ctx, cfg)
    tracked_crypto_symbols = [a["symbol"] for a in ctx.config["watchlist"]["crypto"]]
    now_ts = ctx.now.timestamp()
    max_individual = cfg.get("max_individual_posts_per_call", 3)
    snapshot = {
        "day_context": _day_context(ctx),
        "candidates": candidates,
        "prices": _price_snapshot_lines(ctx),
        "oracle": _oracle_snapshot_lines(ctx),
        "earnings": _earnings_snapshot(ctx),
        "press_releases": _press_releases_snapshot(ctx),
        "own_recent_posts": story_history.recent_texts(ctx.state, now_ts),
        "recent_source_titles": story_history.recent_source_titles(ctx.state, now_ts),
        "individual_post_min_score": cfg.get("individual_post_min_score", 75),
        "digest_min_score": cfg.get("digest_min_score", 45),
        "digest_min_items": cfg.get("digest_min_items", 3),
        "digest_max_items": cfg.get("digest_max_items", 8),
        "max_individual_posts_per_call": max_individual,
        "tracked_crypto_symbols": tracked_crypto_symbols,
    }

    checkpoint_id = _current_checkpoint_id(ctx.now.astimezone(BRUSSELS_TZ))
    decision, usage = ai_manager_brain.decide(snapshot, cfg["model"])
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        # Outright API failure or an unparseable response -- don't mark
        # this checkpoint slot as used on a call that produced nothing
        # usable. checkpoint_attempts tracks how many times THIS slot has
        # failed, so _ready_for_call can retry it on the very next hourly
        # dispatch (2026-07-23: confirmed live that a noon call failing
        # this way otherwise left a ~9-hour hole with no retry until
        # 21:00) -- up to max_attempts_per_checkpoint total tries before
        # giving up until the next real checkpoint. calls_today still
        # increments either way, so a persistently broken API can't retry
        # more than max_calls_per_day times total across the whole day.
        attempts = state["checkpoint_attempts"].get(checkpoint_id, 0) + 1
        state["checkpoint_attempts"][checkpoint_id] = attempts
        max_attempts = cfg.get("max_attempts_per_checkpoint", 4)
        _send_run_summary(
            ctx,
            f"new call failed/unparsed (attempt {attempts}/{max_attempts} this checkpoint, "
            f"{'will retry next hour' if attempts < max_attempts else 'giving up until next checkpoint'})",
            False,
        )
        return False

    # only a successfully parsed decision marks this checkpoint slot as
    # used and clears its failure count, if any
    state["last_call_time"] = ctx.now.timestamp()
    state["last_call_checkpoint"] = checkpoint_id
    state["checkpoint_attempts"].pop(checkpoint_id, None)

    # Full uncapped window, not the prompt's own capped own_recent_posts --
    # cheap local comparison, not prompt tokens, so it can afford to check
    # everything rather than only what fit in the snapshot. Extended below
    # after each accepted post, so a later item in this same batch can't
    # repeat an earlier one in it either.
    prior_texts = story_history.recent_texts(ctx.state, now_ts, limit=None)
    prior_titles = story_history.recent_source_titles(ctx.state, now_ts, limit=None)
    posted_items = []
    declined_items = []
    tracked_set = set(tracked_crypto_symbols)

    for item in (decision.get("posts") or [])[:max_individual]:
        reasoning = item.get("reasoning", "")
        fired_one, detail, candidate = _post_individual_item(
            ctx, item, candidates, prior_texts, prior_titles, cfg, tracked_set
        )
        if fired_one:
            posted_items.append({"text": detail, "reasoning": reasoning})
            prior_texts = prior_texts + [detail]
            if candidate:
                prior_titles = prior_titles + [candidate["title"]]
        else:
            declined_items.append({"reason": detail, "reasoning": reasoning})

    digest = decision.get("digest") or {}
    digest_items = _prepare_digest_items(ctx, cfg, digest, candidates, prior_texts, prior_titles)
    digest_fired = False
    digest_posted_texts = []
    if len(digest_items) >= cfg.get("digest_min_items", 3):
        digest_fired, digest_posted_texts = _post_digest_thread(ctx, digest_items, digest.get("intro"))

    fired = bool(posted_items) or digest_fired
    _send_audit_message(posted_items, declined_items, digest_fired, digest_posted_texts)
    _send_run_summary(
        ctx,
        f"new call ({len(posted_items)} posted, {len(declined_items)} declined, "
        f"digest {'posted' if digest_fired else 'skipped'})",
        fired,
    )
    return fired
