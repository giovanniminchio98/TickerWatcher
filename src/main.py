"""
TickerWatch orchestrator. Runs every trigger in strict priority order so the
most important content (whale alerts, news) always gets a shot at the budget
before lower-priority content (flashback, polls) does. Every trigger call is
wrapped in its own try/except: one broken data source is logged and skipped,
it never takes down the whole run. State is always saved at the end, even on
partial failure, so dedup/budget tracking never goes backwards.

If nothing posts/replies/reposts this run, one Telegram bot-chat message
confirms the pipeline still ran and checked everything -- otherwise a
genuinely quiet run (nothing worth doing) would look identical to a broken
one from the outside.

Toggle a post type off by flipping it to False in ENABLED below.
"""
import logging
import sys

from src import telegram_client
from src.budget import Budget
from src.claude_budget import ClaudeBudget
from src.config import load_all
from src.context import Context
from src.image_budget import ImageBudget
from src.sources import coingecko, cryptoscope_oracle, kraken
from src.state import load_state, save_state
from src.x_client import DRY_RUN, XClient
from src.triggers import (
    ai_manager,
    budget_report,
    comment_engagement,
    content_drafts,
    filler,
    historical_flashback,
    monthly_calendar,
    news_alerts,
    oracle_alerts,
    polls,
    price_alerts,
    reply_manager,
    reply_suggestions,
    retweets,
    scheduled_daily,
    self_reply,
    whale_alerts,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tickerwatch.main")

ENABLED = {
    # paused: mechanical, no-context alerts that could fire back-to-back
    # with nothing tying them together -- doesn't fit the "constant quality,
    # meaningful posts" bar the account is now held to. AI Manager already
    # covers genuinely notable market moves with real explanation attached.
    # Code kept intact -- flip back to True to resume.
    "whale_alerts": False,
    "news_alerts": True,
    "price_alerts": True,
    "oracle_alerts": True,
    "scheduled_daily": True,
    "historical_flashback": True,
    "polls": True,
    "self_reply": True,
    # disabled by default: reposting (retweet/quote-tweet) is a manual,
    # human-only decision now -- ai_manager no longer touches it either.
    # Code kept intact -- flip back to True to resume unconditional
    # retweeting of every new monitored-account post.
    "retweets": False,
    # disabled by default: comment-engagement's role was absorbed by
    # reply_manager, which is itself now also disabled (see below) -- kept
    # off rather than reactivated, since the reply-audience 403 blocks both
    # equally. Code kept intact -- flip back to True if that ever changes.
    "comment_engagement": False,
    "content_drafts": True,
    "ai_manager": True,
    # disabled by default: X's "you must be mentioned or otherwise engaged
    # by the author" reply restriction hit every account we tried,
    # including the smaller reply_only ones added specifically on the
    # theory they'd be more permissive -- confirmed live it's not a
    # per-account setting, it's a blanket API limitation no target account
    # choice gets around. Automated replies are pointless until/unless that
    # changes. Code kept intact -- flip back to True if the restriction
    # ever eases.
    "reply_manager": False,
    # disabled by default: the bot-chat digest message was more noise than
    # signal once manual replying stopped being the primary reply path --
    # code kept intact, flip back to True to resume the digest.
    "reply_suggestions": False,
    # disabled by default: ai_manager's own post decision now absorbs
    # filler's old role (a handful of filler.json's generic-engagement
    # examples are handed to Claude as style reference), but only as an
    # optional, quality-gated fallback -- not a mechanical "always post
    # something." Code kept intact -- flip back to True to restore the old
    # unconditional behavior.
    "filler": False,
    "budget_report": True,
    "monthly_calendar": True,
}


def _safe_run(name, fn, *args):
    if not ENABLED.get(name, True):
        logger.info("[%s] disabled, skipping", name)
        return False
    try:
        result = fn(*args)
        logger.info("[%s] fired=%s", name, bool(result))
        return result
    except Exception:
        logger.exception("[%s] failed, skipping this run", name)
        return False


def _fetch_prices(config):
    coingecko_ids = [c["coingecko_id"] for c in config["watchlist"]["crypto"]]
    try:
        return coingecko.get_simple_prices(coingecko_ids)
    except Exception:
        logger.exception("CoinGecko batch price fetch failed; crypto-dependent triggers will be skipped this run")
        return {}


def _fetch_oracle_data(config):
    """Runs the CryptoScope Oracle (src/sources/cryptoscope_oracle.py, a
    Python port of crypto-scope's oracle.js quant engine) fresh every run,
    for every coin in watchlist.crypto, against Kraken's keyless klines feed
    (src/sources/kraken.py). Originally used Binance, but confirmed live
    that Binance.com's public API returns HTTP 451 for every request from
    GitHub Actions' US-hosted runners -- it never worked here at all, on
    any coin, any run. Kraken has no such restriction on public market
    data. Unlike crypto-scope's own deployment (a once-a-day static
    bundle), this recomputes the full signal/Monte-Carlo read every hourly
    cron run so the verdict never lags real price action. One bad/missing
    symbol is logged and skipped, never takes down the others -- same
    isolation pattern as _fetch_prices and every trigger below."""
    oracle_data = {}
    for asset in config["watchlist"]["crypto"]:
        symbol = asset["symbol"]
        pair = kraken.kraken_pair(symbol)
        try:
            candles = kraken.get_klines(pair, interval="1h", limit=200)
            oracle_data[symbol] = cryptoscope_oracle.analyze(candles)
        except Exception:
            logger.exception("CryptoScope oracle analysis failed for %s (%s)", symbol, pair)
            oracle_data[symbol] = None
    return oracle_data


def main():
    config = load_all()
    state = load_state()
    budget = Budget(state, config["budget"])
    claude_budget = ClaudeBudget(state, config["claude_budget"])
    image_budget = ImageBudget(state, config["image_budget"])
    prices = _fetch_prices(config)
    oracle_data = _fetch_oracle_data(config)
    x_client = XClient()
    ctx = Context(
        config, state, budget, x_client, prices,
        claude_budget=claude_budget, image_budget=image_budget, oracle=oracle_data,
    )

    anything_fired = False
    anything_fired |= bool(_safe_run("whale_alerts", whale_alerts.run, ctx))
    anything_fired |= bool(_safe_run("news_alerts", news_alerts.run, ctx))
    anything_fired |= bool(_safe_run("price_alerts", price_alerts.run, ctx))
    anything_fired |= bool(_safe_run("oracle_alerts", oracle_alerts.run, ctx))
    anything_fired |= bool(_safe_run("scheduled_daily", scheduled_daily.run, ctx))
    anything_fired |= bool(_safe_run("historical_flashback", historical_flashback.run, ctx, anything_fired))
    anything_fired |= bool(_safe_run("polls", polls.run, ctx))
    anything_fired |= bool(_safe_run("self_reply", self_reply.run, ctx))
    anything_fired |= bool(_safe_run("ai_manager", ai_manager.run, ctx))

    # disabled by default (see ENABLED) -- kept callable if re-enabled
    anything_fired |= bool(_safe_run("reply_manager", reply_manager.run, ctx))

    # disabled by default (see ENABLED) -- kept callable if re-enabled
    _safe_run("filler", filler.run, ctx, anything_fired)

    anything_fired |= bool(_safe_run("retweets", retweets.run, ctx))
    anything_fired |= bool(_safe_run("comment_engagement", comment_engagement.run, ctx))

    # Telegram-only draft ideas -- never posts to X, so it never counts
    # toward anything_fired (that would wrongly suppress filler)
    _safe_run("content_drafts", content_drafts.run, ctx)

    # Telegram-only digest of reply candidates across every reply_targets
    # account (big and small alike) -- with reply_manager disabled, manual
    # is the only reply path now, see reply_suggestions.py. Same "never
    # touches X" reasoning as content_drafts, doesn't affect filler
    _safe_run("reply_suggestions", reply_suggestions.run, ctx)

    # Telegram channel only (public, posts-only channel) -- once-a-month
    # earnings calendar, never touches X, same "never affects filler"
    # reasoning as content_drafts/reply_suggestions above
    _safe_run("monthly_calendar", monthly_calendar.run, ctx)

    # independent of the X pipeline/budget above -- always attempted, since
    # this is what tells you when to top up X credits
    _safe_run("budget_report", budget_report.run, ctx)

    if not anything_fired:
        # confirms the pipeline is alive and checked everything even when
        # nothing was worth posting/replying/reposting -- distinguishes a
        # genuinely quiet run from a silently broken one
        telegram_client.send_message("❌ - No posting needed")

    logger.info("Budget: %s", budget.remaining_summary())
    logger.info("Budget: %s", claude_budget.remaining_summary())
    logger.info("Budget: %s", image_budget.remaining_summary())
    if DRY_RUN:
        logger.info("DRY_RUN: not persisting state (dedup/budget bookkeeping stays untouched)")
    else:
        save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Fatal error in TickerWatch run")
        sys.exit(1)
