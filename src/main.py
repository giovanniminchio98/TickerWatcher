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
from src.sources import coingecko
from src.state import load_state, save_state
from src.x_client import DRY_RUN, XClient
from src.triggers import (
    ai_manager,
    budget_report,
    comment_engagement,
    content_drafts,
    filler,
    historical_flashback,
    news_alerts,
    polls,
    price_alerts,
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
    "whale_alerts": True,
    "news_alerts": True,
    "price_alerts": True,
    "scheduled_daily": True,
    "historical_flashback": True,
    "polls": True,
    "self_reply": True,
    # disabled by default: ai_manager now owns the repost decision over the
    # same monitored-accounts pool, with judgment (retweet, quote-tweet with
    # its own comment, or skip) instead of retweeting every new post
    # unconditionally. Code kept intact -- flip back to True to run both.
    "retweets": False,
    # disabled by default: ai_manager now owns the reply decision over the
    # same config/reply_targets.json pool, with more judgment (only replies
    # when it decides a candidate is genuinely worth it, not every time).
    # Code kept intact -- flip back to True to run both side by side.
    "comment_engagement": False,
    "content_drafts": True,
    "ai_manager": True,
    "reply_suggestions": True,
    # disabled by default: ai_manager's own post decision now absorbs
    # filler's old role (a handful of filler.json's generic-engagement
    # examples are handed to Claude as style reference), but only as an
    # optional, quality-gated fallback -- not a mechanical "always post
    # something." Code kept intact -- flip back to True to restore the old
    # unconditional behavior.
    "filler": False,
    "budget_report": True,
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


def main():
    config = load_all()
    state = load_state()
    budget = Budget(state, config["budget"])
    claude_budget = ClaudeBudget(state, config["claude_budget"])
    prices = _fetch_prices(config)
    x_client = XClient()
    ctx = Context(config, state, budget, x_client, prices, claude_budget=claude_budget)

    anything_fired = False
    anything_fired |= bool(_safe_run("whale_alerts", whale_alerts.run, ctx))
    anything_fired |= bool(_safe_run("news_alerts", news_alerts.run, ctx))
    anything_fired |= bool(_safe_run("price_alerts", price_alerts.run, ctx))
    anything_fired |= bool(_safe_run("scheduled_daily", scheduled_daily.run, ctx))
    anything_fired |= bool(_safe_run("historical_flashback", historical_flashback.run, ctx, anything_fired))
    anything_fired |= bool(_safe_run("polls", polls.run, ctx))
    anything_fired |= bool(_safe_run("self_reply", self_reply.run, ctx))
    anything_fired |= bool(_safe_run("ai_manager", ai_manager.run, ctx))

    # disabled by default (see ENABLED) -- kept callable if re-enabled
    _safe_run("filler", filler.run, ctx, anything_fired)

    anything_fired |= bool(_safe_run("retweets", retweets.run, ctx))
    anything_fired |= bool(_safe_run("comment_engagement", comment_engagement.run, ctx))

    # Telegram-only draft ideas -- never posts to X, so it never counts
    # toward anything_fired (that would wrongly suppress filler)
    _safe_run("content_drafts", content_drafts.run, ctx)

    # Telegram-only digest of the biggest reply candidates, for manual replies
    # while X API replies are blocked (see reply_suggestions.py) -- same
    # "never touches X" reasoning as content_drafts, doesn't affect filler
    _safe_run("reply_suggestions", reply_suggestions.run, ctx)

    # independent of the X pipeline/budget above -- always attempted, since
    # this is what tells you when to top up X credits
    _safe_run("budget_report", budget_report.run, ctx)

    if not anything_fired:
        # confirms the pipeline is alive and checked everything even when
        # nothing was worth posting/replying/reposting -- distinguishes a
        # genuinely quiet run from a silently broken one
        telegram_client.send_message(
            "✅ TickerWatch check complete — no post/reply/repost this run "
            "(nothing warranted it). Everything's running fine."
        )

    logger.info("Budget: %s", budget.remaining_summary())
    logger.info("Budget: %s", claude_budget.remaining_summary())
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
