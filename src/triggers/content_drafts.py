"""Post type 10 (opt-in via ANTHROPIC_API_KEY presence): curated, ready-to-post
draft ideas sent ONLY to the private Telegram bot chat -- never to X directly.
Part of a deliberate shift toward a more organic profile: automation surfaces
raw material (notable crypto/stock moves, matching news) and Claude drafts a
short ready-to-post take on each; a human picks/refines/posts a handful
(4-5/day) personally rather than everything going out automatically.

Requires ANTHROPIC_API_KEY -- there's no safe generic fallback for "curated
insight" text, so this trigger simply does nothing without it (same
reasoning as comment_engagement's reply text).

Draws from two pools each run:
  - Notable price moves (crypto watchlist via ctx.prices, stocks/ETFs via
    Twelve Data) -- own cooldown per symbol, separate from price_alerts'
    cooldown, since that trigger still auto-posts to X independently.
  - News articles matching config/keywords.json's feeds/keywords -- own
    dedup list, separate from news_alerts' posted_urls, so the same article
    can legitimately be BOTH auto-posted by news_alerts AND drafted here;
    they serve different purposes (automatic vs. a manually-refined take).

Since posting is manual here (never automatic), a news draft's real source
URL is appended after the drafted text -- no reach/cost penalty to dodge
the way there is on X, so there's no reason to hold it back. Appended by
the trigger itself rather than trusting Claude to reproduce a URL verbatim.

Capped at max_drafts_per_day combined across both pools, and
max_drafts_per_run per run, so a busy run can't flood Telegram."""
import logging

from src import telegram_client
from src.formatting import fmt_pct, fmt_price
from src.sources import draft_writer, news_rss, twelvedata

logger = logging.getLogger("tickerwatch.triggers.content_drafts")


def _roll_daily_count(state, today_str):
    if state.get("posted_date") != today_str:
        state["posted_date"] = today_str
        state["posted_count_today"] = 0


def _remaining_today(ctx):
    cfg = ctx.config["thresholds"]["content_drafts"]
    state = ctx.state["content_drafts"]
    return cfg["max_drafts_per_day"] - state.get("posted_count_today", 0)


def _send_draft(ctx, fact, label, url=None):
    try:
        draft = draft_writer.write_draft(fact)
    except Exception:
        logger.exception("Draft generation failed for: %s", fact)
        return False
    if not draft:
        return False
    text = f"📝 Draft idea ({label}):\n\n{draft}"
    if url:
        # appended programmatically rather than trusting Claude to reproduce
        # a URL verbatim -- posting is manual anyway, so the real link is
        # just here for you to keep or drop when you post
        text += f"\n\n{url}"
    if not telegram_client.send_message(text):
        return False
    ctx.state["content_drafts"]["posted_count_today"] += 1
    return True


def _price_move_candidates(ctx):
    """Yields (fact, cooldown_key) for crypto/stock symbols with a notable
    24h move that hasn't been drafted recently."""
    cfg = ctx.config["thresholds"]["content_drafts"]
    state = ctx.state["content_drafts"]
    now_ts = ctx.now.timestamp()

    for asset in ctx.config["watchlist"]["crypto"]:
        info = ctx.prices.get(asset["coingecko_id"])
        if not info or info.get("usd") is None:
            continue
        price, change = info["usd"], info.get("usd_24h_change")
        yield from _maybe_candidate(state, now_ts, cfg, asset["symbol"], price, change, "crypto")

    for asset in ctx.config["watchlist"]["stocks"]:
        try:
            q = twelvedata.get_quote(asset["symbol"])
        except Exception:
            logger.exception("Twelve Data quote failed for %s", asset["symbol"])
            continue
        if not q:
            continue
        yield from _maybe_candidate(
            state, now_ts, cfg, asset["symbol"], q["price"], q["percent_change"], "stocks/ETFs"
        )


def _maybe_candidate(state, now_ts, cfg, symbol, price, change, kind):
    if change is None or abs(change) < cfg["pct_change_trigger"]:
        return
    last_time = state["last_drafted_time"].get(symbol)
    hours_since = (now_ts - last_time) / 3600 if last_time else None
    if hours_since is not None and hours_since < cfg["min_hours_between_repeat_drafts"]:
        return
    direction = "up" if change >= 0 else "down"
    fact = f"{symbol} is {direction} {fmt_pct(change)} in the last 24h, now trading at ${fmt_price(price)}."
    yield fact, symbol, kind


def run(ctx):
    cfg = ctx.config["thresholds"]["content_drafts"]
    state = ctx.state["content_drafts"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    _roll_daily_count(state, today_str)

    fired = False
    drafted_this_run = 0

    for fact, symbol, kind in _price_move_candidates(ctx):
        if drafted_this_run >= cfg["max_drafts_per_run"] or _remaining_today(ctx) <= 0:
            break
        if _send_draft(ctx, fact, kind):
            state["last_drafted_time"][symbol] = ctx.now.timestamp()
            drafted_this_run += 1
            fired = True

    if drafted_this_run < cfg["max_drafts_per_run"] and _remaining_today(ctx) > 0:
        try:
            articles = news_rss.fetch_matching_articles(
                ctx.config["keywords"]["rss_feeds"],
                ctx.config["keywords"]["keywords"],
                set(state["drafted_urls"]),
                cfg["max_drafts_per_run"] - drafted_this_run,
            )
        except Exception:
            logger.exception("News fetch failed for content drafts")
            articles = []

        for article in articles:
            if drafted_this_run >= cfg["max_drafts_per_run"] or _remaining_today(ctx) <= 0:
                break
            fact = f"News via {article['source']}: {article['title']}. {article['summary']}"
            if _send_draft(ctx, fact, "news", url=article["url"]):
                state["drafted_urls"].append(article["url"])
                drafted_this_run += 1
                fired = True

    state["drafted_urls"] = state["drafted_urls"][-500:]
    return fired
