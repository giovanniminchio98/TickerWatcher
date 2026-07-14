"""Post type 4: always-post-once/day scheduled content -- market snapshot and/or
Fear & Greed Index. Rotates between the two by default (post_both=false in
config/thresholds.json) to keep the monthly post/cost count low; set
post_both=true if your budget allows both every day."""
import logging

from src.formatting import dot_for_change, fmt_pct, fmt_price, thread_parts, truncate
from src.sources import feargreed, twelvedata

logger = logging.getLogger("tickerwatch.triggers.scheduled_daily")


def _build_snapshot_text(ctx):
    watchlist = ctx.config["watchlist"]
    crypto_by_symbol = {c["symbol"]: c for c in watchlist["crypto"]}
    lines = [f"📊 Market Snapshot — {ctx.now.strftime('%b %d, %Y')}"]
    for symbol in watchlist.get("snapshot_order", []):
        if symbol in crypto_by_symbol:
            info = ctx.prices.get(crypto_by_symbol[symbol]["coingecko_id"])
            if not info:
                continue
            price, change = info.get("usd"), info.get("usd_24h_change")
            label = f"${symbol}"
        else:
            try:
                q = twelvedata.get_quote(symbol)
            except Exception:
                logger.exception("Twelve Data quote failed for %s", symbol)
                continue
            if not q:
                continue
            price, change = q["price"], q["percent_change"]
            label = "S&P 500" if symbol == "SPY" else symbol
        lines.append(f"{dot_for_change(change)} {label}: ${fmt_price(price)} ({fmt_pct(change)})")
    if len(lines) <= 1:
        return None
    return "\n".join(lines)


def _build_feargreed_text(ctx):
    try:
        history = feargreed.get_history(limit=8)
    except Exception:
        logger.exception("Fear & Greed fetch failed")
        return None
    if not history:
        return None
    today = history[0]
    yesterday = history[1] if len(history) > 1 else None
    last_week = history[7] if len(history) > 7 else None

    lines = [
        f"{dot_for_change(today['value'] - 50)} Crypto Fear & Greed Index: "
        f"{today['value']} ({today['value_classification']})"
    ]
    if yesterday:
        lines.append(
            f"{dot_for_change(yesterday['value'] - 50)} Yesterday: "
            f"{yesterday['value']} ({yesterday['value_classification']})"
        )
    if last_week:
        lines.append(
            f"{dot_for_change(last_week['value'] - 50)} Last week: "
            f"{last_week['value']} ({last_week['value_classification']})"
        )
    return "\n".join(lines)


def _post_possibly_threaded(ctx, text):
    parts = thread_parts(text)
    reply_to = None
    posted_any = False
    for part in parts:
        if not ctx.budget.can_spend(has_link=False):
            break
        tweet_id = ctx.x.reply(part, reply_to) if reply_to else ctx.x.post(part)
        if not tweet_id:
            break
        ctx.budget.record_spend(has_link=False, text=part)
        reply_to = tweet_id
        posted_any = True
    return posted_any


def run(ctx):
    state = ctx.state["scheduled_daily"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    if state["last_posted_date"] == today_str:
        return False

    post_both = ctx.config["thresholds"]["scheduled_daily"].get("post_both", False)
    if post_both:
        kinds = ["snapshot", "feargreed"]
    else:
        kinds = ["snapshot"] if state["rotate_index"] % 2 == 0 else ["feargreed"]

    fired = False
    for kind in kinds:
        try:
            text = _build_snapshot_text(ctx) if kind == "snapshot" else _build_feargreed_text(ctx)
        except Exception:
            logger.exception("Failed to build %s text", kind)
            continue
        if not text:
            continue
        if _post_possibly_threaded(ctx, truncate(text, max_len=1000)):
            fired = True

    if fired:
        state["last_posted_date"] = today_str
        state["rotate_index"] += 1
    return fired
