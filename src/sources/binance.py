"""
Binance public klines (candlestick) API -- keyless, generous rate limits
(1200 request-weight/min), used as the fresh, per-run OHLC feed behind the
CryptoScope Oracle (src/sources/cryptoscope_oracle.py). This is the same
live-fallback candle source crypto-scope's own site uses when its daily
static bundle isn't warm yet -- here it's the primary source, since the
whole point of running the Oracle inside TickerWatch is a fresh read every
hourly cron run, not once a day.
"""
import logging

import requests

logger = logging.getLogger("tickerwatch.binance")

BASE_URL = "https://api.binance.com/api/v3"
TIMEOUT = 15


def get_klines(symbol, interval="1h", limit=200):
    """symbol is a Binance pair, e.g. "BTCUSDT". Returns a list of
    {"t": ms, "o": float, "h": float, "l": float, "c": float} ascending by
    open time, or [] on any failure -- never raises, so one bad/delisted
    symbol just gets skipped by the oracle rather than breaking the run."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        resp = requests.get(f"{BASE_URL}/klines", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        logger.exception("Binance klines fetch failed for %s (%s)", symbol, interval)
        return []
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
        for r in rows
    ]
