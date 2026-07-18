"""Post type 12: once-a-month earnings calendar, Telegram channel only --
never touches X. Fires once when the calendar month rolls over (no fixed
day-of-month requirement, just "first run after the month changes"), and
never counts toward main.py's anything_fired, same as content_drafts.py/
reply_suggestions.py.

Covers config/financial_calendar.json's ticker list (Big Tech, AI/Semis,
Major Banks) -- deliberately separate from watchlist.json, since it
includes companies (the major banks) not otherwise tracked for price/post
purposes. Uses Twelve Data's existing free-tier earnings_calendar endpoint
(already used by ai_manager.py's _earnings_snapshot) with a date range
covering the whole month, rather than a web-search/Claude-authored
approach -- real structured data from a purpose-built endpoint is more
reliable than search results, and it's free where an extra Claude call (or
worse, a paid search tool call) would not be. Dividend dates were
considered but dropped for now: Twelve Data's free tier dividend support
wasn't confirmed, so this ships earnings-only rather than promising
something unverified.

No fabrication risk: every line comes directly from the API response
(symbol, company name, date) -- nothing here is Claude-authored or
inferred."""
import calendar
import logging
from datetime import datetime

from src import telegram_client
from src.sources import twelvedata

logger = logging.getLogger("tickerwatch.triggers.monthly_calendar")


def _month_bounds(now):
    first = now.date().replace(day=1)
    last_day_num = calendar.monthrange(now.year, now.month)[1]
    last = first.replace(day=last_day_num)
    return first.isoformat(), last.isoformat()


def _tracked_symbols(cfg):
    symbols = set()
    for tickers in cfg.get("categories", {}).values():
        symbols.update(tickers)
    return symbols


def run(ctx):
    state = ctx.state["monthly_calendar"]
    month_str = ctx.now.strftime("%Y-%m")
    if state.get("last_posted_month") == month_str:
        return False

    cfg = ctx.config["financial_calendar"]
    tracked = _tracked_symbols(cfg)
    start_date, end_date = _month_bounds(ctx.now)

    try:
        entries = twelvedata.get_earnings_calendar(start_date, end_date)
    except Exception:
        logger.exception("Twelve Data earnings_calendar fetch failed for monthly_calendar")
        return False

    seen_symbols = set()
    rows = []
    for e in entries:
        symbol = e.get("symbol")
        if not symbol or symbol not in tracked or symbol in seen_symbols:
            continue
        try:
            date_obj = datetime.strptime(e["date"], "%Y-%m-%d")
        except (KeyError, TypeError, ValueError):
            continue
        seen_symbols.add(symbol)
        rows.append((date_obj, symbol, e.get("name") or symbol))

    # only mark the month as done once we've actually confirmed data --
    # a fetch failure returns False above without touching state, so the
    # very next run retries rather than silently skipping the whole month
    state["last_posted_month"] = month_str

    if not rows:
        logger.info("monthly_calendar: no tracked-company earnings found for %s", month_str)
        return False

    rows.sort(key=lambda r: r[0])
    lines = [f"📅 {ctx.now.strftime('%B %Y')} Earnings Calendar\n"]
    for date_obj, symbol, name in rows:
        lines.append(f"{date_obj.strftime('%b %d')} — {name} ({symbol}) — Earnings")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990].rsplit("\n", 1)[0] + "\n…"

    telegram_client.send_channel_message(text)
    return True
