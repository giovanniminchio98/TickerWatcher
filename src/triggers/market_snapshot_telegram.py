"""Post type 12 (Telegram-only, POC 2026-07-23): an hourly stock market
snapshot, paired with a general seasonal-pattern note (day-of-week and
month-of-year) -- built specifically to test Telegram-native content while
X posting stays fully disabled (the account owner is manually curating
posts for a while, external cron stopped). This trigger never imports or
touches ctx.x/ctx.budget at all -- there is no code path here that can
post to X, by construction, not just by a config flag.

Unconditional by design (NOT gated on a notable move, unlike price_alerts.py/
content_drafts.py): sends up to thresholds.market_snapshot's
max_posts_per_run standalone messages every run (never a reply/thread) to
the public-ish Telegram channel, covering the biggest movers among the
tracked symbols regardless of whether the move is large -- this is a
proof-of-concept meant to produce real, visible output right away so the
format can be evaluated and tuned, rather than possibly staying silent for
hours waiting on a threshold. Tune config/thresholds.json's
market_snapshot.symbols/max_posts_per_run once the format is dialed in --
easy candidates: add a real notability gate, widen beyond the 3 default
symbols, or fold in crypto too.

Seasonal notes (config/seasonality.json) are general, well-known
historical calendar tendencies (Santa Claus rally, Sell in May, day-of-
week effects, etc.) for broad US indices -- background context alongside
the real, live price data pulled from Twelve Data, never a prediction for
this specific stock or a guarantee. Same never-fabricate-a-number ethos as
every other trigger: only the seasonal blurb is a canned historical note,
the price/% change is always a real, live figure."""
import logging

from src.formatting import fmt_pct, fmt_price
from src.sources import twelvedata
from src import telegram_client

logger = logging.getLogger("tickerwatch.triggers.market_snapshot_telegram")


def _emoji_for_change(pct):
    if pct is None:
        return "⚪"
    if pct >= 1:
        return "🟢"
    if pct <= -1:
        return "🔴"
    return "🟡"


def _seasonal_note(ctx, seasonality_cfg):
    weekday_name = ctx.now.strftime("%A")
    month_name = ctx.now.strftime("%B")
    lines = []
    month_note = seasonality_cfg.get("months", {}).get(str(ctx.now.month))
    if month_note:
        lines.append(f"📅 {month_name}: {month_note}")
    weekday_note = seasonality_cfg.get("weekdays", {}).get(weekday_name)
    if weekday_note:
        lines.append(f"🗓️ {weekday_name}: {weekday_note}")
    return "\n".join(lines)


def run(ctx):
    cfg = ctx.config["thresholds"]["market_snapshot"]
    symbols = cfg.get("symbols") or [s["symbol"] for s in ctx.config["watchlist"].get("stocks", [])]
    max_posts_per_run = cfg.get("max_posts_per_run", 2)

    quotes = []
    for symbol in symbols:
        try:
            q = twelvedata.get_quote(symbol)
        except Exception:
            logger.exception("market_snapshot_telegram: Twelve Data quote failed for %s", symbol)
            continue
        if q and q.get("price") is not None:
            quotes.append((symbol, q["price"], q.get("percent_change")))

    if not quotes:
        return False

    # biggest movers first (by absolute % change); a symbol with no % data
    # sorts last rather than crashing the comparison
    quotes.sort(key=lambda item: abs(item[2]) if item[2] is not None else -1, reverse=True)
    quotes = quotes[:max_posts_per_run]

    seasonal_note = _seasonal_note(ctx, ctx.config.get("seasonality", {}))

    fired = False
    for symbol, price, pct_change in quotes:
        emoji = _emoji_for_change(pct_change)
        lines = [f"{emoji} {symbol}: ${fmt_price(price)} ({fmt_pct(pct_change)})"]
        if seasonal_note:
            lines.append(seasonal_note)
        text = "\n\n".join(lines)
        if telegram_client.send_channel_message(text):
            fired = True

    return fired
