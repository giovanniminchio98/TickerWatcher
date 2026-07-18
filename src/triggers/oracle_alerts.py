"""Post type: CryptoScope Oracle verdict alerts. Fires only when a tracked
coin's quant signal composite (src/sources/cryptoscope_oracle.py -- the same
Monte-Carlo/technical-signal engine that powers the crypto-scope site,
ported to Python and recomputed fresh every run from Kraken's keyless 1h
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
top of the cooldown.

Deliberately independent of ai_manager's day/night posting cap -- by
design, not an oversight: these are a genuinely different kind of content
(a quant model's own reading of live price data, not editorial judgment),
and the account owner wants them free to fire whenever the signal is
genuinely strong rather than competing with ai_manager's queue for a
shared daily allowance. The one throttle is a flat global
min_minutes_between_any_alert (default 60, i.e. at most one oracle alert
per hour across every coin combined) -- keeps it from bursting multiple
alerts in the same run/hour even if several coins cross the bar at once.

Always opens with the 💰 CRYPTO tag (matching the account's fixed tag
vocabulary; never the urgent JUST IN/BREAKING tags -- a quant signal read
is never "breaking news"), logged to story_history like every other
trigger's posts (so ai_manager's own dedup/Claude judgment is aware of
it), and carries a short disclaimer in the actual post text rather than
only in code comments."""
import logging

from src import story_history
from src.formatting import dot_for_change, fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain

logger = logging.getLogger("tickerwatch.triggers.oracle_alerts")

_VERDICT_EMOJI = {"Strongly Bullish": "🟢🟢", "Strongly Bearish": "🔴🔴"}
_TAG = "💰 CRYPTO"


def run(ctx):
    cfg = ctx.config["thresholds"].get("oracle", {})
    state = ctx.state["oracle_alerts"]
    state.setdefault("last_alert_time_global", None)
    now_ts = ctx.now.timestamp()
    max_per_run = cfg.get("max_alerts_per_run", 1)
    fired = 0

    last_global = state["last_alert_time_global"]
    min_minutes_global = cfg.get("min_minutes_between_any_alert", 60)
    if last_global is not None and (now_ts - last_global) / 60 < min_minutes_global:
        return False

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
            f"{_TAG}: {emoji} ${symbol} Oracle: {label} ({composite['score']}/100, "
            f"{composite['confidence']}% confidence)\n"
            f"{dot_for_change(change_24h)} ${fmt_price(price)} ({fmt_pct(change_24h)} 24h)\n"
            f"{result['regime']['label']} · {round(probs['p_up'] * 100)}% odds up next "
            f"{result['meta']['horizon']}h · statistical read, not advice",
            ai_manager_brain.MAX_POST_LEN,
        )
        tweet_id = ctx.x.post(text)
        if tweet_id:
            ctx.budget.record_spend(has_link=False, text=text)
            story_history.add_entry(ctx.state, text=text, url=None, now_ts=now_ts)
            state["last_alert_time"][symbol] = now_ts
            state["last_alert_label"][symbol] = label
            state["last_alert_time_global"] = now_ts
            fired += 1

    return fired > 0
