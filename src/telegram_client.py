"""
Minimal Telegram Bot API wrapper. Free, and entirely independent of the X API
pipeline/budget -- this is what tells you when the X budget cap is close, so
it must keep working even after the X side goes quiet for the month. Missing/
invalid credentials are logged and skipped, never crash the run.

Two separate destinations:
  - TELEGRAM_CHAT_ID: your private bot chat, for technical messages only
    (budget progress, low-budget alerts, daily recap).
  - TELEGRAM_CHANNEL_ID: a public-ish Telegram channel that mirrors every
    actual post, more generously than X (e.g. links X's posts drop for cost/
    reach reasons get restored here, since Telegram is free). Optional --
    if unset, channel sends are silently skipped, same as missing bot creds.
"""
import logging
import os

import requests

logger = logging.getLogger("tickerwatch.telegram")

BASE_URL = "https://api.telegram.org"
TIMEOUT = 15
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def _send(chat_id_env, text, label):
    if DRY_RUN:
        logger.info("[DRY RUN] would send Telegram %s message:\n%s", label, text)
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get(chat_id_env)
    if not token or not chat_id:
        logger.info("Telegram %s not configured (TELEGRAM_BOT_TOKEN/%s unset), skipping", label, chat_id_env)
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
        logger.exception("Failed to send Telegram %s message", label)
        return False


def send_message(text):
    """Private bot chat -- technical messages (budget/recap/alerts) only."""
    return _send("TELEGRAM_CHAT_ID", text, "bot-chat")


def send_channel_message(text):
    """Public-ish channel -- a full mirror of every post that actually fired."""
    return _send("TELEGRAM_CHANNEL_ID", text, "channel")
