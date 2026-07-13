import json
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")


def _load(name):
    path = os.path.join(CONFIG_DIR, name)
    with open(path, "r") as f:
        return json.load(f)


def load_all():
    return {
        "watchlist": _load("watchlist.json"),
        "keywords": _load("keywords.json"),
        "accounts": _load("accounts.json"),
        "thresholds": _load("thresholds.json"),
        "budget": _load("budget.json"),
    }
