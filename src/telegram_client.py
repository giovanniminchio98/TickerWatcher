"""
Minimal Telegram Bot API wrapper for the daily budget report. Free, and
entirely independent of the X API pipeline/budget -- this is what tells you
when the X budget cap is close, so it must keep working even after the X
side goes quiet for the month. Missing/invalid credentials are logged and
skipped, never crash the run.
"""
import logging
import os

import requests

logger = logging.getLogger("tickerwatch.telegram")

BASE_URL = "https://api.telegram.org"
TIMEOUT = 15
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def send_message(text):
    if DRY_RUN:
        logger.info("[DRY RUN] would send Telegram message:\n%s", text)
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset), skipping report")
        return False
    try:
        resp = requests.post(
            f"{BASE_URL}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Telegram message")
        return False
