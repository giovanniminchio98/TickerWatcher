"""
"Something is broken" safety net -- distinct from budget.py/claude_budget.py's
threshold alerts (those fire when spend is fine but getting close to a cap),
this fires when an X or Claude API call itself fails outright (bad/expired
credentials, API outage, rate limit, exhausted account-side credits) --
failures that would otherwise be silent, since every call site in this
codebase already catches its own exceptions and just returns None/skips by
design (so main.py's per-trigger try/except in _safe_run never sees them).

Sent only to the private Telegram bot chat (telegram_client.send_message),
never the public channel -- this is an operational nudge for you, not
content. At most one alert per failure type (x/claude) per process run --
a single run can hit the same broken dependency many times in a tight loop
(e.g. comment_engagement retrying reply_writer once per tweet), and nobody
needs five identical messages for one underlying problem. Since each
GitHub Actions run is a fresh process, this naturally resets every run --
if the problem persists, you'll hear about it again next run, not every
call within it.
"""
import logging

from src import telegram_client

logger = logging.getLogger("tickerwatch.ops_alerts")

X_CONSOLE_URL = "https://console.x.com/"
CLAUDE_CONSOLE_URL = "https://console.anthropic.com/"

_alerted_this_run = set()


def notify_x_failure(detail):
    _notify("x", f"⚠️ TickerWatch: an X API call failed ({detail}).\nCheck: {X_CONSOLE_URL}")


def notify_claude_failure(detail):
    _notify("claude", f"⚠️ TickerWatch: a Claude API call failed ({detail}).\nCheck: {CLAUDE_CONSOLE_URL}")


def _notify(key, text):
    if key in _alerted_this_run:
        return
    _alerted_this_run.add(key)
    telegram_client.send_message(text)
