"""Post type 4: always-post-once/day scheduled content -- market snapshot,
plus (if config/thresholds.json's scheduled_daily.feargreed_enabled is
true) the Fear & Greed Index rotated in alongside it. Disabled by default
(2026-07-19) -- dropped as one of the less useful posts, market snapshot
now fires alone every day. Code kept intact -- flip feargreed_enabled
back to true to resume the old rotate/post_both behavior."""
import logging

from src.formatting import dot_for_change, fmt_pct, fmt_price, thread_parts, truncate
from src.sources import feargreed, twelvedata

logger = logging.getLogger("tickerwatch.triggers.scheduled_daily")


def _build_snapshot_text(ctx):
    watchlist = ctx.config["watchlist"]
    crypto_by_symbol = {c["symbol"]: c for c in watchlist["crypto"]}
    # US stock markets are closed on weekends -- a stock quote at that point
    # is just Friday's stale close, which would misleadingly sit right next
    # to genuinely live crypto prices as if both were moving right now.
    # Skipping stock lines entirely (rather than caveating each one) is the
    # same weekend reasoning already applied to ai_manager's prompt, just
    # enforced here in code since this trigger is fully mechanical -- no LLM
    # to phrase the caveat itself. Crypto-only snapshots are unaffected.
    is_weekend = ctx.now.weekday() >= 5
    lines = [f"📊 Market Snapshot — {ctx.now.strftime('%b %d, %Y')}"]
    cashtag_used = False
    for symbol in watchlist.get("snapshot_order", []):
        if symbol in crypto_by_symbol:
            info = ctx.prices.get(crypto_by_symbol[symbol]["coingecko_id"])
            if not info:
                continue
            price, change = info.get("usd"), info.get("usd_24h_change")
            # X rejects a post with more than one $cashtag (Forbidden 403), and
            # the snapshot lists several cryptos in one post -- so only the
            # first gets the $ cashtag, the rest fall back to plain text
            if cashtag_used:
                label = symbol
            else:
                label = f"${symbol}"
                cashtag_used = True
        else:
            if is_weekend:
                continue
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

    cfg = ctx.config["thresholds"]["scheduled_daily"]
    if not cfg.get("feargreed_enabled", False):
        kinds = ["snapshot"]
    elif cfg.get("post_both", False):
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
