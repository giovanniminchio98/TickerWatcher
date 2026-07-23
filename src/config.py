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
        "reply_targets": _load("reply_targets.json"),
        "thresholds": _load("thresholds.json"),
        "budget": _load("budget.json"),
        "claude_budget": _load("claude_budget.json"),
        "image_budget": _load("image_budget.json"),
        "ai_manager": _load("ai_manager.json"),
        "reply_manager": _load("reply_manager.json"),
        "reply_suggestions": _load("reply_suggestions.json"),
        "filler": _load("filler.json"),
        "media": _load("media.json"),
        "financial_calendar": _load("financial_calendar.json"),
        "seasonality": _load("seasonality.json"),
    }
