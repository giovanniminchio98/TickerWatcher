"""
Twelve Data free tier: 800 requests/day, 8 requests/minute. Chosen over
Alpha Vantage (free tier cut to 25 requests/day in 2025, too low to check a
watchlist of stocks/ETFs multiple times a day). market_movers requires a
paid Pro+ plan (confirmed, not available here) -- earnings_calendar and
press_releases are both Basic-plan (free) endpoints.
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


def get_earnings_calendar(start_date=None, end_date=None):
    """Companies reporting earnings in the given date range -- today only
    if no range is given (Twelve Data's own default). Free-tier (Basic
    plan) endpoint, unlike market_movers (confirmed Pro+ only, not used
    here). Same "raise on HTTP error, caller catches" pattern as
    get_quote/get_price_on_date -- this one isn't chunked since a calendar
    query is a single request regardless of how many companies it covers.

    Returns a list of dicts: {"symbol", "name", "date", "time",
    "eps_estimate", "eps_actual"} (fields default to None if Twelve Data's
    response doesn't include them). The exact response shape wasn't fully
    confirmed ahead of a live call, so this tolerates either a flat list
    or entries grouped/keyed by date -- an unrecognized shape just yields
    an empty list rather than raising."""
    params = {"apikey": os.environ["TWELVEDATA_API_KEY"]}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    resp = requests.get(f"{BASE_URL}/earnings_calendar", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("status") == "error":
        logger.warning("Twelve Data earnings_calendar error: %s", data.get("message", data))
        return []

    raw = data.get("earnings", data) if isinstance(data, dict) else data
    entries = []
    if isinstance(raw, dict):
        for day_entries in raw.values():
            if isinstance(day_entries, list):
                entries.extend(day_entries)
    elif isinstance(raw, list):
        entries = raw

    results = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("symbol"):
            continue
        results.append({
            "symbol": e.get("symbol"),
            "name": e.get("name"),
            "date": e.get("date"),
            "time": e.get("time"),
            "eps_estimate": e.get("eps_estimate"),
            "eps_actual": e.get("eps_actual"),
        })
    return results


def get_press_releases(symbols=None, max_results=10):
    """Recent official press releases/corporate announcements, optionally
    filtered to specific symbols (comma-joined) -- free-tier (Basic plan)
    endpoint. Same "raise on HTTP error, caller catches" pattern as the
    rest of this module.

    Returns a list of dicts: {"symbol", "title", "date", "url"} -- exact
    response shape wasn't fully confirmed ahead of a live call, so this
    tolerates a few plausible key names and just returns [] rather than
    raising if the shape is unrecognized."""
    params = {"apikey": os.environ["TWELVEDATA_API_KEY"], "outputsize": max_results}
    if symbols:
        params["symbol"] = ",".join(symbols)
    resp = requests.get(f"{BASE_URL}/press_releases", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("status") == "error":
        logger.warning("Twelve Data press_releases error: %s", data.get("message", data))
        return []

    raw = data.get("press_releases", data) if isinstance(data, dict) else data
    entries = raw if isinstance(raw, list) else []

    results = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        results.append({
            "symbol": e.get("symbol"),
            "title": e.get("title") or e.get("headline"),
            "date": e.get("date") or e.get("datetime"),
            "url": e.get("url"),
        })
    return results
