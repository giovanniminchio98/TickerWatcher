"""
Real price-history charts for ai_manager's individual crypto-driven posts
(config/media.json's ai_manager_chart_enabled flag) -- distinct from
media.py/oracle_media.py's static, checked-in generic assets (trend
arrows, coin logos). Crypto-only scope, deliberately: Twelve Data (the
stock price source) is already tightly rate-limited and paced across
ai_manager.py's _price_snapshot_lines (a ~5-minute batch fetch across 30
symbols) -- adding per-story chart calls there risks that budget. CoinGecko
is keyless/cheap and already has an unused market_chart endpoint, so crypto
gets a real chart and stocks don't.

Never raises, anywhere in this module -- any failure (disabled, unknown
symbol, network error, empty series, render error) just means the post
goes out with no image, same "never block a post" contract as
media.py/oracle_media.py.
"""
import io
import logging

from src.sources import coingecko

logger = logging.getLogger("tickerwatch.chart_gen")


def fetch_price_series(coingecko_id, days=14):
    """Returns [(timestamp_ms, price), ...] or None on any failure."""
    try:
        raw = coingecko.get_market_chart(coingecko_id, days=days)
    except Exception:
        logger.exception("CoinGecko market_chart fetch failed for %s", coingecko_id)
        return None
    if not raw:
        return None
    try:
        return [(int(ts), float(price)) for ts, price in raw]
    except (TypeError, ValueError):
        logger.warning("CoinGecko market_chart returned unexpected shape for %s", coingecko_id)
        return None


def render_price_chart(symbol, series, days=14):
    """Renders a simple line chart to an in-memory PNG, returns raw bytes or
    None on any failure. Uses matplotlib's Agg backend -- no display, no
    file I/O, safe on a headless CI runner."""
    if not series:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from datetime import datetime, timezone

        times = [datetime.fromtimestamp(ts / 1000, tz=timezone.utc) for ts, _ in series]
        prices = [price for _, price in series]
        up = prices[-1] >= prices[0]
        color = "#16c784" if up else "#ea3943"

        fig, ax = plt.subplots(figsize=(6, 3), dpi=150)
        ax.plot(times, prices, color=color, linewidth=2)
        ax.fill_between(times, prices, min(prices), color=color, alpha=0.12)
        ax.set_title(f"{symbol} -- last {days}d", fontsize=12, color="#333333", loc="left")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.tick_params(labelsize=8)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render price chart for %s", symbol)
        return None


def generate_chart_for_symbol(ctx, symbol, days=14):
    """Top-level entry point for ai_manager.py: resolves symbol ->
    coingecko_id via config/watchlist.json, fetches + renders, returns PNG
    bytes or None. Gated by config/media.json's ai_manager_chart_enabled --
    same per-feature gate pattern as media.py/oracle_media.py's own flags."""
    if not ctx.config.get("media", {}).get("ai_manager_chart_enabled", True):
        return None
    asset = next((a for a in ctx.config["watchlist"]["crypto"] if a["symbol"] == symbol), None)
    if not asset:
        return None
    series = fetch_price_series(asset["coingecko_id"], days=days)
    if not series:
        return None
    return render_price_chart(symbol, series, days=days)
