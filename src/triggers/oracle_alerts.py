"""Post type: CryptoScope Oracle posts. Two interchangeable modes, switched
by a single config key (config/thresholds.json's oracle.mode, "alert" or
"rotation") -- no code change needed to go back and forth, both modes share
every helper below (_format_post, _meets_alert_bar, the emoji maps), only
the top-level orchestration in run() differs:

- "alert" (the original design): posts only when a tracked coin's quant
  signal composite (src/sources/cryptoscope_oracle.py -- the same
  Monte-Carlo/technical-signal engine that powers the crypto-scope site,
  recomputed fresh every run from Kraken's keyless 1h klines, see
  ctx.oracle / main.py's _fetch_oracle_data) reads Bullish/Bearish or
  stronger with real signal agreement (confidence >=
  thresholds.oracle.min_confidence). Deduped per coin by
  min_hours_between_alerts AND by verdict label, so a repeat only fires
  once the read has actually changed since the last alert, on top of the
  cooldown. Quiet on Neutral/Lean readings and low-confidence scores.

- "rotation" (current default): posts exactly one coin's current snapshot
  every run, cycling through every coin in watchlist.crypto in a fixed
  round-robin order (state.oracle_alerts.rotation_index) -- guarantees a
  post every run regardless of how strong the signal is, so every coin
  gets covered on a predictable schedule (once every len(coins) runs).
  The same 🚨/⚠️ escalation prefix from alert mode still only appears when
  the alert-mode bar (_meets_alert_bar) is genuinely met, so a real signal
  still visually stands out among the routine snapshots around it.

Confirmed live that gating on "Strongly Bullish/Bearish" alone (score
>=72 or <=28) almost never fired against real market data (scores mostly
sit in the 40-50 Neutral/Lean range) -- alert mode's bar is Bullish/
Bearish or stronger (score >=60 or <=40) instead. Single emoji (🟢/🔴)
marks the regular Bullish/Bearish tier, double (🟢🟢/🔴🔴) marks Strongly,
⚪ marks Neutral (rotation mode only -- alert mode never posts Neutral).

Deliberately independent of ai_manager's day/night posting cap in both
modes -- these are a genuinely different kind of content (a quant model's
own reading of live price data, not editorial judgment), not competing
with ai_manager's queue for a shared allowance. The one throttle in
either mode is a flat global min_minutes_between_any_alert (default 60,
i.e. at most one Oracle post per hour across every coin combined) -- in
rotation mode this is what turns "one post per hourly cron run" into a
real cap rather than just an assumption, and in both modes it guards
against a manual re-trigger double-posting inside the same hour.

Always opens with its own 💰 CRYPTO 🔮 tag on its own line (the 🔮
distinguishes it from a routine ai_manager crypto post at a glance --
never the urgent JUST IN/BREAKING tags, a quant signal read is never
"breaking news"), logged to story_history like every other trigger's
posts (so ai_manager's own dedup/Claude judgment is aware of it), and
carries a short disclaimer in the actual post text rather than only in
code comments."""
import logging

from src import story_history
from src.formatting import dot_for_change, fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain

logger = logging.getLogger("tickerwatch.triggers.oracle_alerts")

_VERDICT_EMOJI = {
    "Strongly Bullish": "🟢🟢", "Bullish": "🟢", "Lean Bullish": "🟢",
    "Neutral": "⚪",
    "Lean Bearish": "🔴", "Bearish": "🔴", "Strongly Bearish": "🔴🔴",
}
_ALERT_LABELS = {"Strongly Bullish", "Bullish", "Strongly Bearish", "Bearish"}
_STRONG_LABELS = {"Strongly Bullish", "Strongly Bearish"}
# 🔮 on top of the plain CRYPTO tag distinguishes an Oracle read from a
# routine ai_manager crypto post at a glance, on its own opening line.
_TAG = "💰 CRYPTO 🔮"


def _meets_alert_bar(label, confidence, cfg):
    return label in _ALERT_LABELS and confidence >= cfg.get("min_confidence", 35)


def _escalation_prefix(label, confidence, cfg):
    """🚨 for a Strongly read that clears the bar, ⚠️ for a regular
    Bullish/Bearish read that clears it, nothing otherwise -- this is what
    keeps a genuine signal visually distinct from the routine snapshots
    around it in rotation mode (alert mode only ever posts bar-clearing
    reads anyway, so it always gets a prefix)."""
    if not _meets_alert_bar(label, confidence, cfg):
        return ""
    return "🚨 " if label in _STRONG_LABELS else "⚠️ "


def _format_post(cfg, symbol, result, price, change_24h):
    composite = result["composite"]
    label = composite["label"]
    confidence = composite["confidence"]
    probs = result["probs"]
    emoji = _VERDICT_EMOJI.get(label, "⚪")
    prefix = _escalation_prefix(label, confidence, cfg)
    return truncate(
        f"{_TAG}:\n"
        f"{prefix}{emoji} ${symbol} Oracle: {label} ({composite['score']}/100, "
        f"{confidence}% confidence)\n"
        f"{dot_for_change(change_24h)} ${fmt_price(price)} ({fmt_pct(change_24h)} 24h)\n"
        f"{result['regime']['label']} · {round(probs['p_up'] * 100)}% odds up next "
        f"{result['meta']['horizon']}h · statistical read, not advice",
        ai_manager_brain.MAX_POST_LEN,
    )


def _record_post(ctx, state, symbol, label, text, now_ts):
    ctx.budget.record_spend(has_link=False, text=text)
    story_history.add_entry(ctx.state, text=text, url=None, now_ts=now_ts)
    state["last_alert_time"][symbol] = now_ts
    state["last_alert_label"][symbol] = label
    state["last_alert_time_global"] = now_ts


def run(ctx):
    cfg = ctx.config["thresholds"].get("oracle", {})
    state = ctx.state["oracle_alerts"]
    state.setdefault("last_alert_time_global", None)
    state.setdefault("rotation_index", 0)

    now_ts = ctx.now.timestamp()
    last_global = state["last_alert_time_global"]
    min_minutes_global = cfg.get("min_minutes_between_any_alert", 60)
    if last_global is not None and (now_ts - last_global) / 60 < min_minutes_global:
        return False

    if cfg.get("mode", "alert") == "rotation":
        return _run_rotation(ctx, cfg, state, now_ts)
    return _run_alert(ctx, cfg, state, now_ts)


def _run_alert(ctx, cfg, state, now_ts):
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
        if not _meets_alert_bar(label, composite["confidence"], cfg):
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
        text = _format_post(cfg, symbol, result, price, change_24h)
        tweet_id = ctx.x.post(text)
        if tweet_id:
            _record_post(ctx, state, symbol, label, text, now_ts)
            fired += 1

    return fired > 0


def _run_rotation(ctx, cfg, state, now_ts):
    """Posts exactly one coin's current snapshot per run, regardless of
    how strong the signal is. Walks forward from state.rotation_index,
    skipping (but still consuming the slot of) any coin with no data this
    run, so a single missing fetch never costs the hour's guaranteed post."""
    coins = ctx.config["watchlist"]["crypto"]
    if not coins:
        return False
    if not ctx.budget.can_spend(has_link=False):
        return False

    start_index = state["rotation_index"] % len(coins)
    for offset in range(len(coins)):
        index = (start_index + offset) % len(coins)
        state["rotation_index"] = (index + 1) % len(coins)
        asset = coins[index]
        symbol = asset["symbol"]
        result = ctx.oracle.get(symbol)
        if not result:
            continue

        price = result["meta"]["price"]
        change_24h = ctx.prices.get(asset["coingecko_id"], {}).get("usd_24h_change")
        text = _format_post(cfg, symbol, result, price, change_24h)
        tweet_id = ctx.x.post(text)
        if not tweet_id:
            return False
        _record_post(ctx, state, symbol, result["composite"]["label"], text, now_ts)
        return True

    return False
