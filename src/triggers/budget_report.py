"""
Daily budget recap -- fires once/day at 9pm Europe/Brussels time (zoneinfo
handles the CET/CEST switch automatically, no manual DST math needed).
Deliberately independent of the X posting pipeline's priority/budget gating
so it keeps working even after the monthly X budget cap trips, since that's
exactly when you need the nudge to top up.
"""
import logging
from zoneinfo import ZoneInfo

from src import telegram_client

logger = logging.getLogger("tickerwatch.triggers.budget_report")

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")
REPORT_HOUR = 21  # 9pm


def run(ctx):
    brussels_now = ctx.now.astimezone(BRUSSELS_TZ)
    if brussels_now.hour != REPORT_HOUR:
        return False

    state = ctx.state["telegram"]
    today_str = brussels_now.strftime("%Y-%m-%d")
    if state["last_report_date"] == today_str:
        return False

    b = ctx.state["budget"]
    cfg = ctx.config["budget"]

    if cfg["mode"] == "posts":
        used, cap = b["posts_used"], cfg["monthly_post_cap"]
        pct = (used / cap * 100) if cap else 0
        text = f"📅 Daily recap\n{used}/{cap} posts ({pct:.0f}% used)"
    else:
        used, cap = b["usd_used"], cfg["monthly_usd_cap"]
        pct = (used / cap * 100) if cap else 0
        text = f"📅 Daily recap\n${used:.2f}/${cap:.2f} ({pct:.0f}% used)"

    sent = telegram_client.send_message(text)
    if sent:
        state["last_report_date"] = today_str
    return sent
