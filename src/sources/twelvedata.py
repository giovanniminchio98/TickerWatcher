"""
Twelve Data free tier: 800 requests/day, 8 requests/minute. Chosen over
Alpha Vantage (free tier cut to 25 requests/day in 2025, too low to check a
watchlist of stocks/ETFs multiple times a day). market_movers requires a
paid Pro+ plan (confirmed, not available here) -- earnings_calendar and
press_releases are both Basic-plan (free) endpoints.
"""
import logging
import os
import time

import requests

logger = logging.getLogger("tickerwatch.twelvedata")

BASE_URL = "https://api.twelvedata.com"
TIMEOUT = 15
QUOTE_CHUNK_SIZE = 5
QUOTE_CHUNK_PAUSE_SECONDS = 15


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
    """Fetches many symbols in small chunks (QUOTE_CHUNK_SIZE, comma-joined
    per request) with a pause between chunks (QUOTE_CHUNK_PAUSE_SECONDS),
    rather than one big request -- confirmed live that even a 30-symbol
    batch in one shot can 429 against Twelve Data's free-tier per-minute
    limit. Each chunk is independent: a chunk that fails (429 or anything
    else) is logged and skipped, never aborting the rest -- a partial
    result (whatever chunks succeeded) beats nothing. Never raises.

    Pacing is deliberately modest (not a full minute between chunks) to
    stay well inside the workflow's 10-minute job timeout even in the
    worst case (6 chunks for 30 symbols = 5 pauses = ~75s added) --
    accepting a higher miss rate on any given chunk in exchange for not
    risking the whole run (Claude call, posting, state commit) getting
    killed by the timeout.

    Returns {symbol: {"price": float, "percent_change": float}, ...} --
    any symbol not present (bad ticker, per-symbol error, or its whole
    chunk failing) is just absent from the dict."""
    if not symbols:
        return {}

    quotes = {}
    for i in range(0, len(symbols), QUOTE_CHUNK_SIZE):
        chunk = symbols[i : i + QUOTE_CHUNK_SIZE]
        if i > 0:
            time.sleep(QUOTE_CHUNK_PAUSE_SECONDS)
        try:
            params = {"symbol": ",".join(chunk), "apikey": os.environ["TWELVEDATA_API_KEY"]}
            resp = requests.get(f"{BASE_URL}/quote", params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Twelve Data quote chunk failed for %s", chunk)
            continue

        # a single-symbol request returns one flat quote object instead of
        # a dict keyed by symbol -- normalize to the keyed shape
        if len(chunk) == 1:
            data = {chunk[0]: data}

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
