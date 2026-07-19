"""Attaches a coin logo + a green/red trend chart to oracle_alerts.py posts
(user-supplied images, checked into assets/oracle/ -- see that directory).
Scoped to oracle_alerts only, gated by config/media.json's oracle_enabled
flag, independent of the separate CoinGecko-logo gate in the same file.

Never raises and never blocks a post: any failure along the way (disabled,
unknown symbol, read error, upload error) just means the post goes out
with fewer/no images, same as before this feature existed.
"""
import logging
import os

logger = logging.getLogger("tickerwatch.oracle_media")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "oracle")

_COIN_LOGOS = {
    "BTC": os.path.join(ASSETS_DIR, "btc.jpeg"),
    "ETH": os.path.join(ASSETS_DIR, "eth.png"),
    "SOL": os.path.join(ASSETS_DIR, "sol.jpeg"),
    "XRP": os.path.join(ASSETS_DIR, "xrp.png"),
}
_TREND_CHARTS = {
    "up": os.path.join(ASSETS_DIR, "trend_up.jpeg"),
    "down": os.path.join(ASSETS_DIR, "trend_down.jpeg"),
}


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        logger.exception("Failed to read oracle media asset %s", path)
        return None


def _trend_direction(label):
    """Bullish/Bearish (any strength) drives the chart pick -- alert mode
    never posts anything else. Falls back to None (no chart) for a
    Neutral/Lean-only read, e.g. if rotation mode is ever turned back on."""
    if "Bullish" in label:
        return "up"
    if "Bearish" in label:
        return "down"
    return None


def get_media_ids(ctx, symbol, label):
    """Returns a list of up to 2 media_id_strings (coin logo, trend chart)
    for oracle_alerts.py's ctx.x.post(media_ids=...), or [] if disabled/
    unavailable -- callers should treat an empty list exactly like no
    media at all."""
    if not ctx.config.get("media", {}).get("oracle_enabled", True):
        return []

    media_ids = []
    logo_path = _COIN_LOGOS.get(symbol)
    if logo_path:
        image_bytes = _read_bytes(logo_path)
        if image_bytes:
            media_id = ctx.x.upload_media(image_bytes)
            if media_id:
                media_ids.append(media_id)

    direction = _trend_direction(label)
    chart_path = _TREND_CHARTS.get(direction)
    if chart_path:
        image_bytes = _read_bytes(chart_path)
        if image_bytes:
            media_id = ctx.x.upload_media(image_bytes)
            if media_id:
                media_ids.append(media_id)

    return media_ids
