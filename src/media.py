"""Shared helper for attaching a themed trend icon to a post.

Gated by config/media.json's 'enabled' flag so it can be switched off
instantly (no code change, just a config push) if a live billing check ever
shows X charging extra for posts with media -- see that file's comment.

Never raises and never blocks a post: any failure along the way (disabled,
no sentiment, read error, upload error) just means the post goes out
without media, same as it did before this feature existed.
"""
import logging
import os

logger = logging.getLogger("tickerwatch.media")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
TREND_ICONS = {
    "up": os.path.join(ASSETS_DIR, "trend_up.png"),
    "down": os.path.join(ASSETS_DIR, "trend_down.png"),
    "neutral": os.path.join(ASSETS_DIR, "trend_neutral.png"),
}


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
