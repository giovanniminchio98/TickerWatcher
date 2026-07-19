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
either mode is "have we already posted in this UTC calendar hour"
(state.oracle_alerts.last_alert_hour_id, "%Y-%m-%d-%H") -- at most one
Oracle post per hour across every coin combined, and in rotation mode
this is what turns "one post per hourly cron run" into a real guarantee.

Deliberately a fixed hour-bucket check, NOT an elapsed-minutes timer
(e.g. "60 minutes since the last post") -- confirmed live that an
elapsed-time throttle can skip an entire hour: the external cron doesn't
fire at exactly :00:00 every time, it lands a few seconds to a minute
into the hour, so a post at :00:51 followed by next hour's cron at
:00:39 is only 59m48s apart and would get wrongly blocked by a strict
60-minute timer. A calendar-hour bucket has no such race: it only cares
whether the current run's hour differs from the last post's hour, so it
can never skip a genuinely new hour no matter how the exact seconds
land. Same lesson already learned once this session for ai_manager's
call cadence (elapsed-time+jitter drifted; fixed clock checkpoints
didn't) -- applied here as fixed calendar hours rather than fixed
clock-of-day checkpoints, since this trigger runs on every cron tick.

Always opens with its own 💰 CRYPTO 🔮 tag on its own line (the 🔮
distinguishes it from a routine ai_manager crypto post at a glance --
never the urgent JUST IN/BREAKING tags, a quant signal read is never
"breaking news"), logged to story_history like every other trigger's
posts (so ai_manager's own dedup/Claude judgment is aware of it), and
carries a short disclaimer in the actual post text rather than only in
code comments.

Four post-style builders exist (_STYLE_BODY_BUILDERS), picked at random
per post from whichever subset is listed in _STYLES so consecutive posts
don't all look identical -- each surfaces a different slice of the same
already-computed Oracle result, no new data fetching involved:
- "snapshot" (the original design): price + 24h change + regime + P(up)
- "levels": nearest support/resistance from the fractal-pivot detector
- "forecast": Monte Carlo touch-probabilities and a 90% price range, with
  the actual horizon stated explicitly (never a bare, unscoped number)
- "momentum": RSI/MACD/Stochastic with plain-language overbought/
  oversold/neutral framing
Currently only "snapshot" and "forecast" are active in _STYLES -- "levels"
and "momentum" are deliberately kept implemented but unused (not deleted)
so they're a one-line _STYLES change away from coming back, rather than
needing to be rewritten if wanted again later.
All four share the same header line (tag, verdict emoji, escalation
prefix, score/confidence) and the same closing disclaimer -- only the
middle body differs, so a real signal is exactly as visible regardless
of which style happened to be picked.

Every post also carries up to 2 images (src/oracle_media.py): the coin's
own logo (assets/oracle/{btc,eth,sol,xrp}.*) plus a green "up" or red
"down" trend chart picked from the verdict's Bullish/Bearish direction
(assets/oracle/trend_{up,down}.jpeg) -- both user-supplied, checked into
the repo. Gated by config/media.json's oracle_enabled flag, independent
of every other trigger's own media decision."""
import logging
import random

from src import oracle_media, story_history
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
# "levels" and "momentum" are implemented (see _STYLE_BODY_BUILDERS below)
# but deliberately left out of the active pool -- add them back here if wanted.
_STYLES = ("snapshot", "forecast")
_STYLE_TITLES = {
    "snapshot": "Oracle", "levels": "Levels", "forecast": "Forecast", "momentum": "Momentum",
}


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


def _snapshot_body(result, price, change_24h):
    probs = result["probs"]
    return (
        f"{dot_for_change(change_24h)} ${fmt_price(price)} ({fmt_pct(change_24h)} 24h)\n"
        f"{result['regime']['label']} · {round(probs['p_up'] * 100)}% odds up next "
        f"{result['meta']['horizon']}h"
    )


def _levels_body(result, price, change_24h):
    levels = result["levels"]
    resistances = levels.get("resistances") or []
    supports = levels.get("supports") or []
    res_str = " / ".join(f"${fmt_price(v)}" for v in resistances) if resistances else "none nearby"
    sup_str = " / ".join(f"${fmt_price(v)}" for v in supports) if supports else "none nearby"
    return (
        f"${fmt_price(price)} ({fmt_pct(change_24h)} 24h)\n"
        f"🔴 Resistance: {res_str}\n"
        f"🟢 Support: {sup_str}"
    )


def _forecast_body(result, price, change_24h):
    horizon = result["meta"]["horizon"]
    targets = result["probs"]["targets"]
    up5 = next((t for t in targets if t["label"] == "+5%"), None)
    dn5 = next((t for t in targets if t["label"] == "−5%"), None)
    range_lo = price * (1 + result["probs"]["range_lo"])
    range_hi = price * (1 + result["probs"]["range_hi"])
    touch_line = ""
    if up5 and dn5:
        touch_line = (
            f"{round(up5['touch'] * 100)}% chance to touch +5%, "
            f"{round(dn5['touch'] * 100)}% chance to touch -5%\n"
        )
    return (
        f"${fmt_price(price)} · next {horizon}h forecast:\n"
        f"{touch_line}"
        f"90% range: ${fmt_price(range_lo)} – ${fmt_price(range_hi)}"
    )


def _rsi_label(v):
    if v is None:
        return "n/a"
    if v > 70:
        return f"{round(v)} (overbought)"
    if v < 30:
        return f"{round(v)} (oversold)"
    return f"{round(v)} (neutral)"


def _stoch_label(v):
    if v is None:
        return "n/a"
    if v > 80:
        return f"{round(v)} (overbought)"
    if v < 20:
        return f"{round(v)} (oversold)"
    return f"{round(v)} (neutral)"


def _momentum_body(result, price, change_24h):
    momentum = result["momentum"]
    macd = momentum.get("macd")
    macd_label = "n/a"
    if macd:
        macd_label = "bullish" if macd["hist"] > 0 else ("bearish" if macd["hist"] < 0 else "flat")
    return (
        f"${fmt_price(price)} ({fmt_pct(change_24h)} 24h)\n"
        f"RSI {_rsi_label(momentum.get('rsi'))} · MACD {macd_label} · "
        f"Stoch {_stoch_label(momentum.get('stoch'))}"
    )


_STYLE_BODY_BUILDERS = {
    "snapshot": _snapshot_body, "levels": _levels_body,
    "forecast": _forecast_body, "momentum": _momentum_body,
}


def _format_post(cfg, symbol, result, price, change_24h, style=None):
    style = style or random.choice(_STYLES)
    composite = result["composite"]
    label = composite["label"]
    confidence = composite["confidence"]
    emoji = _VERDICT_EMOJI.get(label, "⚪")
    prefix = _escalation_prefix(label, confidence, cfg)
    header = (
        f"{prefix}{emoji} ${symbol} {_STYLE_TITLES[style]}: {label} "
        f"({composite['score']}/100, {confidence}% confidence)"
    )
    body = _STYLE_BODY_BUILDERS[style](result, price, change_24h)
    return truncate(
        f"{_TAG}:\n{header}\n{body}\nstatistical read, not advice",
        ai_manager_brain.MAX_POST_LEN,
    )


def _record_post(ctx, state, symbol, label, text, now_ts, hour_id):
    ctx.budget.record_spend(has_link=False, text=text)
    story_history.add_entry(ctx.state, text=text, url=None, now_ts=now_ts)
    state["last_alert_time"][symbol] = now_ts
    state["last_alert_label"][symbol] = label
    state["last_alert_hour_id"] = hour_id


def run(ctx):
    cfg = ctx.config["thresholds"].get("oracle", {})
    state = ctx.state["oracle_alerts"]
    state.setdefault("last_alert_hour_id", None)
    state.setdefault("rotation_index", 0)

    now_ts = ctx.now.timestamp()
    hour_id = ctx.now.strftime("%Y-%m-%d-%H")
    if state["last_alert_hour_id"] == hour_id:
        return False

    if cfg.get("mode", "alert") == "rotation":
        return _run_rotation(ctx, cfg, state, now_ts, hour_id)
    return _run_alert(ctx, cfg, state, now_ts, hour_id)


def _run_alert(ctx, cfg, state, now_ts, hour_id):
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
        media_ids = oracle_media.get_media_ids(ctx, symbol, label)
        tweet_id = ctx.x.post(text, media_ids=media_ids)
        if tweet_id:
            _record_post(ctx, state, symbol, label, text, now_ts, hour_id)
            fired += 1

    return fired > 0


def _run_rotation(ctx, cfg, state, now_ts, hour_id):
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
        label = result["composite"]["label"]
        text = _format_post(cfg, symbol, result, price, change_24h)
        media_ids = oracle_media.get_media_ids(ctx, symbol, label)
        tweet_id = ctx.x.post(text, media_ids=media_ids)
        if not tweet_id:
            return False
        _record_post(ctx, state, symbol, label, text, now_ts, hour_id)
        return True

    return False
