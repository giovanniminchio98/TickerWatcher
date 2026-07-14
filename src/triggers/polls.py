"""Post type 6: engagement poll, scheduled ~1-2x/week (config: days_of_week,
0=Monday..6=Sunday). Uses X API v2 poll creation via tweepy's poll_options/
poll_duration_minutes kwargs on create_tweet."""
import logging
from datetime import timedelta

from src.formatting import fmt_price

logger = logging.getLogger("tickerwatch.triggers.polls")


def _nearest_round_number(price, milestones):
    above = [m for m in milestones if m > price]
    if above:
        return min(above)
    # no configured milestone above current price: round up to a sensible magnitude
    magnitude = 10 ** (len(str(int(price))) - 1)
    return ((int(price) // magnitude) + 1) * magnitude


def run(ctx):
    cfg = ctx.config["thresholds"]["polls"]
    state = ctx.state["polls"]

    if ctx.now.weekday() not in cfg["days_of_week"]:
        return False
    today_str = ctx.now.strftime("%Y-%m-%d")
    if state["last_posted_date"] == today_str:
        return False

    symbol = cfg["asset"]
    asset_cfg = next((c for c in ctx.config["watchlist"]["crypto"] if c["symbol"] == symbol), None)
    if not asset_cfg:
        logger.warning("Poll asset %s not found in watchlist", symbol)
        return False
    price = ctx.prices.get(asset_cfg["coingecko_id"], {}).get("usd")
    if not price:
        return False

    milestones = ctx.config["thresholds"]["price"]["milestones"].get(symbol, [])
    round_number = _nearest_round_number(price, milestones)
    target_date = ctx.now + timedelta(days=cfg["horizon_days"])
    day_name = target_date.strftime("%A")

    if not ctx.budget.can_spend(has_link=False):
        return False

    text = f"${symbol} above or below ${fmt_price(round_number)} by {day_name}? 👇"
    tweet_id = ctx.x.post(text, poll_options=["Above", "Below"], poll_duration_minutes=cfg["horizon_days"] * 1440)
    if tweet_id:
        ctx.budget.record_spend(has_link=False, text=text)
        state["last_posted_date"] = today_str
        return True
    return False
