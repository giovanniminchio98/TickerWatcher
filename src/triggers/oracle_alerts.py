"""Post type: CryptoScope Oracle verdict alerts. Fires only when a tracked
coin's quant signal composite (src/sources/cryptoscope_oracle.py -- the same
Monte-Carlo/technical-signal engine that powers the crypto-scope site,
ported to Python and recomputed fresh every run from Binance's keyless 1h
klines, see ctx.oracle / main.py's _fetch_oracle_data) reaches a genuinely
strong reading -- Strongly Bullish/Bearish with real signal agreement
(confidence >= thresholds.oracle.min_confidence) -- not on every score
wobble. Deliberately conservative: this is a statistical read of price
history, not a guarantee, so it only speaks up when the model itself is
confident, and stays quiet on Neutral/Lean readings and low-confidence
scores.

Deduped per coin by config/thresholds.json's oracle.min_hours_between_alerts
AND by verdict label: re-alerting the same label back-to-back (e.g. still
"Strongly Bullish" an hour later) would just be noise, so a repeat only
fires once the model's read has actually flipped since the last alert, on
top of the cooldown."""
import logging

from src.formatting import dot_for_change, fmt_pct, fmt_price, truncate

logger = logging.getLogger("tickerwatch.triggers.oracle_alerts")

_VERDICT_EMOJI = {"Strongly Bullish": "🟢🟢", "Strongly Bearish": "🔴🔴"}


def run(ctx):
    cfg = ctx.config["thresholds"].get("oracle", {})
    state = ctx.state["oracle_alerts"]
    now_ts = ctx.now.timestamp()
    max_per_run = cfg.get("max_alerts_per_run", 1)
    fired = 0

    for asset in ctx.config["watchlist"]["crypto"]:
        if fired >= max_per_run:
            break
        symbol = asset["symbol"]
        result = ctx.oracle.get(symbol)
        if not result:
            continue

        composite = result["composite"]
        label = composite["label"]
        if label not in _VERDICT_EMOJI:
            continue
        if composite["confidence"] < cfg.get("min_confidence", 60):
            continue

        last_time = state["last_alert_time"].get(symbol)
        last_label = state["last_alert_label"].get(symbol)
        hours_since = (now_ts - last_time) / 3600 if last_time else None
        already_cooling_down = (
            last_label == label and hours_since is not None
            and hours_since < cfg.get("min_hours_between_alerts", 12)
        )
        if already_cooling_down:
            continue

        if not ctx.budget.can_spend(has_link=False):
            break

        price = result["meta"]["price"]
        change_24h = ctx.prices.get(asset["coingecko_id"], {}).get("usd_24h_change")
        probs = result["probs"]
        emoji = _VERDICT_EMOJI[label]

        text = truncate(
            f"{emoji} Oracle read on ${symbol}: {label} ({composite['score']}/100, "
            f"{composite['confidence']}% confidence)\n"
            f"{dot_for_change(change_24h)} ${fmt_price(price)} ({fmt_pct(change_24h)} 24h)\n"
            f"{result['regime']['label']} · {round(probs['p_up'] * 100)}% odds up over the next "
            f"{result['meta']['horizon']}h"
        )
        tweet_id = ctx.x.post(text)
        if tweet_id:
            ctx.budget.record_spend(has_link=False, text=text)
            state["last_alert_time"][symbol] = now_ts
            state["last_alert_label"][symbol] = label
            fired += 1

    return fired > 0
