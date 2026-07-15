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


def get_quotes_batch(symbols):
    """Same shape as get_quote, but for many symbols in ONE HTTP request
    (comma-joined) -- one call at N symbols still costs N of the free
    tier's 800 API credits/day, but it's a single request rather than N,
    which is what actually matters for the 8 requests/minute rate limit.
    Callers fetching a broad list (e.g. AI Manager's ~50-stock universe)
    should always use this instead of looping get_quote() per symbol.

    Returns {symbol: {"price": float, "percent_change": float}, ...} --
    a symbol missing from the result (bad ticker, per-symbol error) is
    just absent from the dict rather than failing the whole batch."""
    if not symbols:
        return {}
    params = {"symbol": ",".join(symbols), "apikey": os.environ["TWELVEDATA_API_KEY"]}
    resp = requests.get(f"{BASE_URL}/quote", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    # a single-symbol request returns one flat quote object instead of a
    # dict keyed by symbol -- normalize so callers always get the keyed shape
    if len(symbols) == 1:
        data = {symbols[0]: data}

    quotes = {}
    for symbol, entry in data.items():
        if not isinstance(entry, dict) or entry.get("status") == "error" or "close" not in entry:
            logger.warning("Twelve Data batch error for %s: %s", symbol, entry)
            continue
        quotes[symbol] = {
            "price": float(entry["close"]),
            "percent_change": float(entry["percent_change"]),
        }
    return quotes


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
