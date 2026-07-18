"""Kraken public OHLC (candlestick) API -- keyless, used as the fresh,
per-run candle feed behind the CryptoScope Oracle
(src/sources/cryptoscope_oracle.py), replacing src/sources/binance.py.

Confirmed live that Binance.com's public API returns HTTP 451 ("Unavailable
For Legal Reasons") for every request from GitHub Actions' US-hosted
runners -- Binance blocks US-origin IPs on its main API by policy, so it
never worked here at all, on any coin, any run. Kraken has no such
restriction on its public market-data endpoints, and Kraken's EU entity is
MiCA-licensed, so it's a solid choice on both fronts (the account owner is
in the EU; the runner making the request is in the US).

Kraken uses minute-based intervals (not "1h" strings) and legacy asset
tickers for some coins (BTC -> XBT) -- see _kraken_pair. The OHLC endpoint's
result dict is keyed by Kraken's own internal pair name, which doesn't
always match the input pair string verbatim (e.g. "XBTUSD" in, "XXBTZUSD"
back out), so the one non-"last" key is taken generically rather than
looked up by name.
"""
import logging

import requests

logger = logging.getLogger("tickerwatch.kraken")

BASE_URL = "https://api.kraken.com/0/public"
TIMEOUT = 15

_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}

# Kraken's legacy ticker for Bitcoin -- every other tracked symbol (ETH,
# SOL, XRP) matches its usual ticker as Kraken's pair prefix.
_SYMBOL_TO_KRAKEN = {"BTC": "XBT"}


def kraken_pair(symbol, quote="USD"):
    base = _SYMBOL_TO_KRAKEN.get(symbol, symbol)
    return f"{base}{quote}"


def get_klines(pair, interval="1h", limit=200):
    """pair is a Kraken asset pair altname, e.g. "XBTUSD" (see kraken_pair).
    Returns a list of {"t": ms, "o": float, "h": float, "l": float,
    "c": float} ascending by open time, trimmed to the most recent `limit`
    bars, or [] on any failure -- never raises, same contract
    cryptoscope_oracle.analyze() and main.py's _fetch_oracle_data already
    expect from the old binance.get_klines."""
    params = {"pair": pair, "interval": _INTERVAL_MINUTES.get(interval, 60)}
    try:
        resp = requests.get(f"{BASE_URL}/OHLC", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("Kraken OHLC fetch failed for %s (%s)", pair, interval)
        return []

    if data.get("error"):
        logger.warning("Kraken OHLC error for %s: %s", pair, data["error"])
        return []

    result = data.get("result", {})
    series = next((v for k, v in result.items() if k != "last"), None)
    if not series:
        return []

    candles = [
        {"t": int(row[0]) * 1000, "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])}
        for row in series
    ]
    return candles[-limit:]
