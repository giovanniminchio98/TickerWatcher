"""Shared helpers for attaching post media (coin logos, themed trend icons).

Gated by config/media.json's 'enabled' flag so both can be switched off
instantly (no code change, just a config push) if a live billing check ever
shows X charging extra for posts with media -- see that file's comment.

Never raises and never blocks a post: any failure along the way (disabled,
no image, download/read error, upload error) just means the post goes out
without media, same as it did before this feature existed.
"""
import logging
import os

from src.sources import coingecko

logger = logging.getLogger("tickerwatch.media")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
TREND_ICONS = {
    "up": os.path.join(ASSETS_DIR, "trend_up.png"),
    "down": os.path.join(ASSETS_DIR, "trend_down.png"),
    "neutral": os.path.join(ASSETS_DIR, "trend_neutral.png"),
}


def get_coin_media_id(ctx, coingecko_id):
    if not coingecko_id or not ctx.config.get("media", {}).get("enabled", True):
        return None
    image_url = ctx.prices.get(coingecko_id, {}).get("image")
    if not image_url:
        return None
    try:
        image_bytes = coingecko.get_image_bytes(image_url)
    except Exception:
        logger.exception("Coin image fetch failed for %s", coingecko_id)
        return None
    if not image_bytes:
        return None
    return ctx.x.upload_media(image_bytes)


def get_trend_media_id(ctx, sentiment):
    """sentiment is 'up'/'down'/'neutral'/None -- attaches a small static
    red/green/gray trend-line graphic (assets/trend_*.png, generated once,
    checked into the repo) for post types that aren't tied to one coin's
    logo, e.g. news alerts. No network call needed, just a local file read."""
    if not sentiment or not ctx.config.get("media", {}).get("enabled", True):
        return None
    path = TREND_ICONS.get(sentiment)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            image_bytes = f.read()
    except Exception:
        logger.exception("Failed to read trend icon %s", path)
        return None
    return ctx.x.upload_media(image_bytes)
