"""Post type 7: self-reply updates on today's own price/milestone alert tweets.
Safe by construction -- only ever replies to this bot's own tweets, never to
other accounts' content."""
import logging

from src.formatting import dot_for_change, fmt_pct, fmt_price
from src.sources import twelvedata

logger = logging.getLogger("tickerwatch.triggers.self_reply")


def _crypto_cfg(ctx, symbol):
    return next((c for c in ctx.config["watchlist"]["crypto"] if c["symbol"] == symbol), None)


def _current_price(ctx, symbol):
    crypto_cfg = _crypto_cfg(ctx, symbol)
    if crypto_cfg:
        info = ctx.prices.get(crypto_cfg["coingecko_id"])
        return info.get("usd") if info else None
    try:
        q = twelvedata.get_quote(symbol)
    except Exception:
        logger.exception("Twelve Data quote failed for %s", symbol)
        return None
    return q["price"] if q else None


def run(ctx):
    cfg = ctx.config["thresholds"]["self_reply"]
    state = ctx.state["self_reply"]
    now_ts = ctx.now.timestamp()

    remaining = []
    replies_done = 0
    fired = False

    for item in state["pending"]:
        age_hours = (now_ts - item["posted_at"]) / 3600

        if replies_done >= cfg["max_replies_per_run"]:
            remaining.append(item)
            continue
        if age_hours < cfg["min_hours_after_post"]:
            remaining.append(item)
            continue
        if age_hours > cfg["max_hours_after_post"]:
            continue  # follow-up window missed; drop rather than post a stale update

        new_price = _current_price(ctx, item["symbol"])
        if new_price is None:
            remaining.append(item)
            continue

        if not ctx.budget.can_spend(has_link=False):
            remaining.append(item)
            continue

        pct = (new_price - item["price"]) / item["price"] * 100
        trend_emoji = "📈" if pct >= 0 else "📉"
        display_symbol = f"${item['symbol']}" if _crypto_cfg(ctx, item["symbol"]) else item["symbol"]
        text = (
            f"Update: {display_symbol} now at ${fmt_price(new_price)}, "
            f"{dot_for_change(pct)} {fmt_pct(pct)} since this morning's alert {trend_emoji}"
        )
        reply_id = ctx.x.reply(text, item["tweet_id"])
        if reply_id:
            ctx.budget.record_spend(has_link=False, text=text)
            replies_done += 1
            fired = True
        else:
            remaining.append(item)

    state["pending"] = remaining
    return fired
