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
import random
import re

from src import telegram_client
from src.formatting import fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain, news_rss, twelvedata

CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")

logger = logging.getLogger("tickerwatch.triggers.ai_manager")

# how many posts' worth of news URLs to remember and exclude from future
# snapshots -- comfortably covers "not again within the next several
# posts" without permanently blocking a story that's still relevant later
RECENT_NEWS_URLS_CAP = 12


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
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0
        state["posts_today"] = 0


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


def _news_snapshot(ctx, state, limit=6):
    """Excludes articles referenced by a post within roughly the last
    RECENT_NEWS_URLS_CAP posts (state["recent_news_urls"]) -- without this,
    the same real story could resurface call after call, since RSS feeds
    keep serving the same entries until they age out on the source's end.
    Not a permanent block: the list is a rolling window, so a story is fair
    game again once it's rolled off (a different day/week revisit is fine,
    even good)."""
    kw_cfg = ctx.config["keywords"]
    already_used = set(state.get("recent_news_urls", []))
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


def _drain_queue(ctx, cfg, state):
    """Fires at most one queued post per run -- the hourly cron cadence
    itself is what spreads a batch's posts across the day, no extra Claude
    call needed. Returns True if something was actually posted."""
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

    if state["posts_today"] >= cfg["max_posts_per_day"]:
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
    state["recent_post_texts"] = (state.get("recent_post_texts", []) + [text])[-10:]
    state["posts_today"] += 1
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

    fired = _drain_queue(ctx, cfg, state)

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
        "news": _news_snapshot(ctx, state),
        "earnings": _earnings_snapshot(ctx),
        "press_releases": _press_releases_snapshot(ctx),
        "own_recent_posts": state.get("recent_post_texts", []),
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

    # trim to what the daily cap can actually still take -- no point queuing
    # (and having Claude write) a post that will just sit until it expires
    remaining_today = max(0, cfg["max_posts_per_day"] - state["posts_today"] - len(state["post_queue"]))
    take = min(cfg.get("posts_per_batch", 1), remaining_today)

    queued_items = []
    declined_posts = []
    recent_news_urls = state.setdefault("recent_news_urls", [])
    for post in (decision.get("posts") or [])[:take]:
        if not post.get("should_post") or not post.get("text"):
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
        if link_url:
            # tracked at queue time (not fire time) so the very next call in
            # this same run cycle can't re-surface the same article either
            recent_news_urls.append(link_url)
    state["recent_news_urls"] = recent_news_urls[-RECENT_NEWS_URLS_CAP:]
    state["post_queue"].extend(queued_items)

    _send_audit_message(queued_items, declined_posts)

    # fire the first queued item right away rather than waiting for the next
    # hourly tick, so a fresh batch doesn't just sit until then
    if state["post_queue"]:
        fired = _drain_queue(ctx, cfg, state) or fired

    _send_run_summary(state, f"new batch ({len(queued_items)} queued)", fired)
    return fired
