"""
Twelve Data free tier: 800 requests/day, 8 requests/minute. Chosen over
Alpha Vantage (free tier cut to 25 requests/day in 2025, too low to check a
watchlist of stocks/ETFs multiple times a day).
"""
import logging
import os

import requests

logger = logging.getLogger("tickerwatch.twelvedata")

BASE_URL = "https://api.twelvedata.com"
TIMEOUT = 15


def get_quote(symbol):
    """Returns {"price": float, "percent_change": float} or None on failure."""
    params = {"symbol": symbol, "apikey": os.environ["TWELVEDATA_API_KEY"]}
    resp = requests.get(f"{BASE_URL}/quote", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error" or "close" not in data:
        logger.warning("Twelve Data error for %s: %s", symbol, data.get("message", data))
        return None
    return {
        "price": float(data["close"]),
        "percent_change": float(data["percent_change"]),
    }


def get_price_on_date(symbol, date_str):
    """date_str must be YYYY-MM-DD. Returns float close price or None."""
    params = {
        "symbol": symbol,
        "interval": "1day",
        "start_date": date_str,
        "end_date": date_str,
        "apikey": os.environ["TWELVEDATA_API_KEY"],
    }
    resp = requests.get(f"{BASE_URL}/time_series", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    values = data.get("values") or []
    if not values:
        return None
    return float(values[0]["close"])
