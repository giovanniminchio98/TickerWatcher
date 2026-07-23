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
    """Returns {coingecko_id: {"usd": float, "usd_24h_change": float}}"""
    if not coingecko_ids:
        return {}
    params = {
        "ids": ",".join(coingecko_ids),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    resp = requests.get(f"{BASE_URL}/simple/price", params=params, headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


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


def get_market_chart(coingecko_id, days=14, vs_currency="usd"):
    """Returns [[timestamp_ms, price], ...] over the trailing `days` days --
    used by src/sources/chart_gen.py to render ai_manager's crypto price
    charts. Same raise-on-HTTP-error/caller-catches shape as
    get_price_on_date -- this module never swallows its own errors, callers
    decide how to degrade."""
    params = {"vs_currency": vs_currency, "days": days}
    resp = requests.get(
        f"{BASE_URL}/coins/{coingecko_id}/market_chart", params=params, headers=_headers(), timeout=TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("prices", [])
