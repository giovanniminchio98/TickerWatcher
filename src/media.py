"""Shared helper: fetch a coin's official CoinGecko-hosted logo and upload it
to X as post media, for triggers that want to attach a coin icon to a post.

Gated by config/media.json's 'enabled' flag so it can be switched off
instantly (no code change, just a config push) if a live billing check ever
shows X charging extra for posts with media -- see that file's comment.

Never raises and never blocks a post: any failure along the way (disabled,
no image URL, download error, upload error) just means the post goes out
without media, same as it did before this feature existed.
"""
import logging

from src.sources import coingecko

logger = logging.getLogger("tickerwatch.media")


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
