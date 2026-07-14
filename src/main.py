"""
TickerWatch orchestrator. Runs every trigger in strict priority order so the
most important content (whale alerts, news) always gets a shot at the budget
before filler content (flashback, polls) does. Every trigger call is wrapped
in its own try/except: one broken data source is logged and skipped, it never
takes down the whole run. State is always saved at the end, even on partial
failure, so dedup/budget tracking never goes backwards.

Toggle a post type off by flipping it to False in ENABLED below.
"""
import logging
import sys

from src.budget import Budget
from src.config import load_all
from src.context import Context
from src.sources import coingecko
from src.state import load_state, save_state
from src.x_client import DRY_RUN, XClient
from src.triggers import (
    budget_report,
    comment_engagement,
    content_drafts,
    filler,
    historical_flashback,
    news_alerts,
    polls,
    price_alerts,
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
    "retweets": True,
    "comment_engagement": True,
    "content_drafts": True,
    "filler": True,
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
    prices = _fetch_prices(config)
    x_client = XClient()
    ctx = Context(config, state, budget, x_client, prices)

    anything_fired = False
    anything_fired |= bool(_safe_run("whale_alerts", whale_alerts.run, ctx))
    anything_fired |= bool(_safe_run("news_alerts", news_alerts.run, ctx))
    anything_fired |= bool(_safe_run("price_alerts", price_alerts.run, ctx))
    anything_fired |= bool(_safe_run("scheduled_daily", scheduled_daily.run, ctx))
    anything_fired |= bool(_safe_run("historical_flashback", historical_flashback.run, ctx, anything_fired))
    anything_fired |= bool(_safe_run("polls", polls.run, ctx))
    anything_fired |= bool(_safe_run("self_reply", self_reply.run, ctx))

    # last resort: only posts if nothing above did, so the account still
    # posts roughly once per check instead of going silent on quiet hours
    _safe_run("filler", filler.run, ctx, anything_fired)

    _safe_run("retweets", retweets.run, ctx)
    _safe_run("comment_engagement", comment_engagement.run, ctx)

    # Telegram-only draft ideas -- never posts to X, so it never counts
    # toward anything_fired (that would wrongly suppress filler)
    _safe_run("content_drafts", content_drafts.run, ctx)

    # independent of the X pipeline/budget above -- always attempted, since
    # this is what tells you when to top up X credits
    _safe_run("budget_report", budget_report.run, ctx)

    logger.info("Budget: %s", budget.remaining_summary())
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
