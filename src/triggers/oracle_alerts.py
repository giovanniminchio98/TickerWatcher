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

Shares the exact same account-wide day/night posting cap as ai_manager.py
(same ctx.state["ai_manager"] window_id/window_posts bookkeeping, same
config/ai_manager.json day_max_posts/night_max_posts/pacing) rather than
having its own separate budget -- confirmed live this trigger originally
bypassed the cap entirely, letting it post outside the pacing the rest of
the account is held to. Reuses ai_manager's own window helpers directly
instead of duplicating that logic (safe: ai_manager.py never imports this
module, so there's no cycle). Always opens with the 💰 CRYPTO tag (never
the urgent JUST IN/BREAKING tags -- a quant signal read is never "breaking
news", so it should never skip the pacing cap the way real urgent news
can), logged to story_history like every other trigger's posts (so
ai_manager's own dedup/Claude judgment sees it too), and carries a short
disclaimer in the actual post text rather than only in code comments."""
import logging

from src import story_history
from src.formatting import dot_for_change, fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain
from src.triggers import ai_manager

logger = logging.getLogger("tickerwatch.triggers.oracle_alerts")

_VERDICT_EMOJI = {"Strongly Bullish": "🟢🟢", "Strongly Bearish": "🔴🔴"}
_TAG = "💰 CRYPTO"


def run(ctx):
    cfg = ctx.config["thresholds"].get("oracle", {})
    am_cfg = ctx.config["ai_manager"]
    state = ctx.state["oracle_alerts"]
    am_state = ctx.state["ai_manager"]
    now_ts = ctx.now.timestamp()
    max_per_run = cfg.get("max_alerts_per_run", 1)
    fired = 0

    window_id, window_kind, window_start, window_end = ai_manager._current_window(ctx, am_cfg)
    ai_manager._roll_window(am_state, window_id)

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

        # never urgent -- a quant signal read never earns the JUST IN/
        # BREAKING cap exception, it always respects the normal pacing
        allowed = ai_manager._paced_cap_for(window_kind, am_cfg, window_start, window_end, ctx.now, urgent=False)
        if am_state["window_posts"] >= allowed:
            logger.info("oracle_alerts: declining %s alert, day/night posting cap reached", symbol)
            break

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
            am_state["window_posts"] += 1
            state["last_alert_time"][symbol] = now_ts
            state["last_alert_label"][symbol] = label
            fired += 1

    return fired > 0
