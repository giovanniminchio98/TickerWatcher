"""
CoinGecko free API. Works keyless (public endpoint, tighter/undocumented rate
limiting) or, if COINGECKO_API_KEY is set (free Demo plan key -- 10k calls/mo,
100 calls/min, no credit card), sends it as the x-cg-demo-api-key header for a
more reliable quota. See README for how to get a free Demo key.
"""
import logging
import os

import requests

logger = logging.getLogger("tickerwatch.coingecko")

BASE_URL = "https://api.coingecko.com/api/v3"
TIMEOUT = 15


def _headers():
    key = os.environ.get("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def get_simple_prices(coingecko_ids):
    """Returns {coingecko_id: {"usd": float, "usd_24h_change": float, "image": str|None}}.
    Uses /coins/markets rather than /simple/price so the coin's official logo
    URL comes back in the same call already made for prices every run --
    no extra request/quota spent to support attaching it to alert posts."""
    if not coingecko_ids:
        return {}
    params = {
        "vs_currency": "usd",
        "ids": ",".join(coingecko_ids),
    }
    resp = requests.get(f"{BASE_URL}/coins/markets", params=params, headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return {
        row["id"]: {
            "usd": row.get("current_price"),
            "usd_24h_change": row.get("price_change_percentage_24h"),
            "image": row.get("image"),
        }
        for row in resp.json()
    }


def get_image_bytes(image_url):
    """Downloads a coin logo's raw bytes for uploading to X as post media.
    Returns None on any failure so a broken/slow image never blocks a post."""
    if not image_url:
        return None
    try:
        resp = requests.get(image_url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.content
    except Exception:
        logger.exception("Failed to download coin image from %s", image_url)
        return None


def get_price_on_date(coingecko_id, date_str):
    """date_str must be dd-mm-yyyy. Returns float USD price or None."""
    params = {"date": date_str, "localization": "false"}
    resp = requests.get(
        f"{BASE_URL}/coins/{coingecko_id}/history", params=params, headers=_headers(), timeout=TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return float(data["market_data"]["current_price"]["usd"])
    except (KeyError, TypeError):
        return None
