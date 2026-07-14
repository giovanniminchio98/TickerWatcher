"""Post type 3: price threshold / milestone alerts across the crypto + stock/ETF
watchlist. Crypto prices come from the batch CoinGecko call already done once
per run (ctx.prices); stock/ETF quotes come from Twelve Data (one call per
symbol -- fine within its 800/day, 8/min free-tier limits at this run frequency)."""
import logging

from src.formatting import dot_for_change, fmt_pct, fmt_price, truncate
from src.sources import twelvedata

logger = logging.getLogger("tickerwatch.triggers.price")


def _get_quote(ctx, symbol, coingecko_id):
    if coingecko_id:
        info = ctx.prices.get(coingecko_id)
        if not info:
            return None
        return info.get("usd"), info.get("usd_24h_change")
    try:
        q = twelvedata.get_quote(symbol)
    except Exception:
        logger.exception("Twelve Data quote failed for %s", symbol)
        return None
    if not q:
        return None
    return q["price"], q["percent_change"]


def _crossed_milestone(last_price, price, milestones):
    if last_price is None:
        return None
    for m in milestones:
        if (last_price < m <= price) or (last_price > m >= price):
            return m
    return None


def run(ctx):
    th = ctx.config["thresholds"]["price"]
    state = ctx.state["price"]
    now_ts = ctx.now.timestamp()
    fired = False

    assets = [(c["symbol"], c["coingecko_id"], "crypto") for c in ctx.config["watchlist"]["crypto"]]
    assets += [(s["symbol"], None, "stock") for s in ctx.config["watchlist"]["stocks"]]

    for symbol, coingecko_id, kind in assets:
        quote = _get_quote(ctx, symbol, coingecko_id)
        if quote is None:
            continue
        price, change_24h = quote

        last_price = state["last_alert_price"].get(symbol)
        last_time = state["last_alert_time"].get(symbol)
        hours_since = (now_ts - last_time) / 3600 if last_time else None

        milestones = th["milestones"].get(symbol, [])
        crossed = _crossed_milestone(last_price, price, milestones)

        should_alert = False
        if crossed is not None:
            should_alert = True
        elif (
            last_price is not None
            and hours_since is not None
            and hours_since >= th["min_hours_between_repeat_alerts"]
        ):
            pct_diff = abs(price - last_price) / last_price * 100
            should_alert = pct_diff >= th["pct_change_trigger"]

        if last_price is None:
            # first time tracking this asset: record a baseline, don't alert on it
            state["last_alert_price"][symbol] = price
            continue

        if not should_alert:
            continue

        if not ctx.budget.can_spend(has_link=False):
            break

        hashtag = "#Crypto" if kind == "crypto" else "#Stocks"
        text = truncate(
            f"🚨 {symbol} just crossed ${fmt_price(price)}\n"
            f"{dot_for_change(change_24h)} 24h change: {fmt_pct(change_24h)}\n#{symbol} {hashtag}"
        )
        tweet_id = ctx.x.post(text)
        if tweet_id:
            ctx.budget.record_spend(has_link=False, text=text)
            state["last_alert_price"][symbol] = price
            state["last_alert_time"][symbol] = now_ts
            ctx.register_self_reply_candidate(symbol, price, tweet_id)
            fired = True

    return fired
