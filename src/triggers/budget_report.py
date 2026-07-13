"""
Daily Telegram budget report -- deliberately independent of the X posting
pipeline's priority/budget gating (it always runs once/day regardless of
whether the X budget cap has been hit), since its whole purpose is telling
you when to top up X credits.
"""
import logging

from src import telegram_client

logger = logging.getLogger("tickerwatch.triggers.budget_report")


def run(ctx):
    state = ctx.state["telegram"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    if state["last_report_date"] == today_str:
        return False

    b = ctx.state["budget"]
    cfg = ctx.config["budget"]
    daily = b.get("daily", {"posts_used": 0, "usd_used": 0.0})

    if cfg["mode"] == "posts":
        today_line = f"Today: {daily['posts_used']} posts"
        month_line = f"Month-to-date: {b['posts_used']}/{cfg['monthly_post_cap']} posts"
    else:
        today_line = f"Today: ${daily['usd_used']:.2f} ({daily['posts_used']} posts)"
        month_line = f"Month-to-date: ${b['usd_used']:.2f} / ${cfg['monthly_usd_cap']:.2f} cap"

    text = f"📊 TickerWatch budget report — {today_str}\n{today_line}\n{month_line}"

    sent = telegram_client.send_message(text)
    if sent:
        state["last_report_date"] = today_str
    return sent
