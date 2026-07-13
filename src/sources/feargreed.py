"""Free, keyless Crypto Fear & Greed Index API (alternative.me)."""
import logging

import requests

logger = logging.getLogger("tickerwatch.feargreed")

BASE_URL = "https://api.alternative.me/fng/"
TIMEOUT = 15


def get_history(limit=8):
    """Returns a list of {"value": int, "value_classification": str, "timestamp": int},
    newest first."""
    resp = requests.get(BASE_URL, params={"limit": limit, "format": "json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()["data"]
    return [
        {
            "value": int(d["value"]),
            "value_classification": d["value_classification"],
            "timestamp": int(d["timestamp"]),
        }
        for d in data
    ]
