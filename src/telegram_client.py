"""
Minimal Telegram Bot API wrapper. Free, and entirely independent of the X API
pipeline/budget -- this is what tells you when the X budget cap is close, so
it must keep working even after the X side goes quiet for the month. Missing/
invalid credentials are logged and skipped, never crash the run.

Three separate destinations:
  - TELEGRAM_CHAT_ID: your private bot chat -- operational messages only
    (reply suggestions, the AI Manager audit, quiet-run heartbeat, outright
    API failure alerts). No cost/budget numbers here anymore, see below.
  - TELEGRAM_CHANNEL_ID: a public-ish Telegram channel that mirrors every
    actual post, more generously than X (e.g. links X's posts drop for cost/
    reach reasons get restored here, since Telegram is free). Also gets the
    real image whenever a post has one, even though X's own extras ratio
    means only some posts carry one -- Telegram has no such limit, so any
    image/link a post has is always shown here. Optional -- if unset,
    channel sends are silently skipped, same as missing bot creds.
  - TELEGRAM_COST_CHAT_ID: your private cost-tracking chat -- every dollar
    figure lives here instead (per-post budget progress, daily recap,
    low-budget/recharge-credits alerts across all three budgets: X, Claude,
    and image generation). Split out from the bot chat specifically so
    operational noise and financial tracking don't get tangled together.
    Optional -- if unset, falls back to the bot chat so cost visibility
    never silently disappears just because this wasn't configured yet.
"""
import html
import logging
import os

import requests

logger = logging.getLogger("tickerwatch.telegram")

BASE_URL = "https://api.telegram.org"
TIMEOUT = 15
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def escape_html(text):
    """Escape free-text (paraphrased headlines, etc.) before embedding it in
    an HTML-parsed Telegram message -- an unescaped &/</> in real article
    text (e.g. "AT&T", "Q&A") would otherwise make Telegram reject the whole
    message."""
    return html.escape(text, quote=False)


def link_html(label, url):
    """A short, tappable link with no auto-expanded preview card bloating
    the message -- pair with disable_web_page_preview (already the default
    in _send) so only this short label shows, not the raw URL."""
    return f'<a href="{url}">{escape_html(label)}</a>'


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
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Telegram %s message", label)
        return False


def _send_photo(chat_id_env, image_bytes, caption, label):
    if DRY_RUN:
        logger.info("[DRY RUN] would send Telegram %s photo, caption:\n%s", label, caption)
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get(chat_id_env)
    if not token or not chat_id:
        logger.info("Telegram %s not configured (TELEGRAM_BOT_TOKEN/%s unset), skipping", label, chat_id_env)
        return False
    try:
        resp = requests.post(
            f"{BASE_URL}/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("post.png", image_bytes)},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Telegram %s photo", label)
        return False


def send_message(text):
    """Private bot chat -- technical messages (budget/recap/alerts) only."""
    return _send("TELEGRAM_CHAT_ID", text, "bot-chat")


def send_channel_message(text):
    """Public-ish channel -- a full mirror of every post that actually fired."""
    return _send("TELEGRAM_CHANNEL_ID", text, "channel")


def send_channel_photo(image_bytes, caption):
    """Public-ish channel -- forwards the same image a post got on X (when it
    got one) as a real photo with the post text as caption, since Telegram
    has no reason to skip it the way X's 1-in-4 extras ratio does."""
    return _send_photo("TELEGRAM_CHANNEL_ID", image_bytes, caption, "channel")


def send_cost_message(text):
    """Private cost-tracking chat -- every dollar figure (budget progress,
    daily recap, low-budget/recharge alerts) lives here instead of the bot
    chat. Falls back to the bot chat if TELEGRAM_COST_CHAT_ID isn't set, so
    cost visibility never just silently disappears."""
    if not os.environ.get("TELEGRAM_COST_CHAT_ID"):
        return _send("TELEGRAM_CHAT_ID", text, "bot-chat (cost fallback)")
    return _send("TELEGRAM_COST_CHAT_ID", text, "cost-chat")
