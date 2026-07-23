"""Post type 12 (Telegram-only, POC 2026-07-23): an hourly stock market
snapshot, paired with a general seasonal-pattern note (day-of-week and
month-of-year) -- built specifically to test Telegram-native content while
X posting stays fully disabled (the account owner is manually curating
posts for a while, external cron stopped). This trigger never imports or
touches ctx.x/ctx.budget at all -- there is no code path here that can
post to X, by construction, not just by a config flag.

Unconditional by design (NOT gated on a notable move, unlike price_alerts.py/
content_drafts.py): sends up to thresholds.market_snapshot's
max_posts_per_run standalone messages every run (never a reply/thread) to
the public-ish Telegram channel, covering the biggest movers among the
tracked symbols regardless of whether the move is large -- this is a
proof-of-concept meant to produce real, visible output right away so the
format can be evaluated and tuned, rather than possibly staying silent for
hours waiting on a threshold.

Symbols default to watchlist.stocks_broad (2026-07-23: widened from an
initial 3-symbol POC to the same 30-symbol universe ai_manager already
uses -- "the scope added once in this repo" per the account owner) --
fetched via twelvedata.get_quotes_batch, NOT a per-symbol loop: that
function's own chunking (5 symbols/request) and 60s pause between chunks
is the only thing keeping this under Twelve Data's free-tier 8-requests/
minute limit. 30 symbols costs ~5 minutes of runtime per hourly run (6
chunks, 5 pauses) -- see main.py's _TRIGGER_TIMEOUTS override for this
trigger. Going substantially higher (50-100 symbols) would cost 9-19
minutes and risks colliding with the whole job's 15-minute ceiling,
especially on the 4 checkpoint hours where ai_manager's own 30-symbol
fetch runs in the same job -- if more coverage is wanted later, either
round-robin a larger list across runs or confirm Twelve Data's endpoint
can actually take a larger single-request symbol count before widening
QUOTE_CHUNK_SIZE itself.

Message templating (added 2026-07-23, replacing a single fixed format):
every message is picked from a bank of several phrasings for its move-size
scenario (strong/mild gain, flat, mild/strong loss), so consecutive hourly
posts don't all read identically even when covering the same symbol
repeatedly. Per-symbol state (ctx.state["market_snapshot_telegram"]) tracks
the last template used for that symbol and excludes it from the next pick,
so the same phrasing never fires twice in a row for the same stock.

Session-phase aware: converts to US/Eastern to tag pre-market/after-hours
moves distinctly (regular session gets no extra tag, since that's the
common case) -- genuinely "specific to the day" context a bare price/%
line doesn't carry. Skips the run ENTIRELY on weekends (no US equity
session at all) rather than repeating Friday's now-stale close every hour
through the weekend -- this is the main defense against the "same message
over and over" duplicate problem, on top of the template variety.

Seasonal notes (config/seasonality.json) are general, well-known
historical calendar tendencies (Santa Claus rally, Sell in May, day-of-
week effects, etc.) for broad US indices -- background context alongside
the real, live price data pulled from Twelve Data, never a prediction for
this specific stock or a guarantee. Same never-fabricate-a-number ethos as
every other trigger: only the seasonal blurb/template phrasing is canned,
the price/% change is always a real, live figure."""
import logging
import random
from datetime import time
from zoneinfo import ZoneInfo

from src.formatting import fmt_pct, fmt_price
from src.sources import twelvedata
from src import telegram_client

logger = logging.getLogger("tickerwatch.triggers.market_snapshot_telegram")

US_EASTERN = ZoneInfo("America/New_York")

# Move-size scenario buckets, most extreme first -- pct is always the
# real, live 24h/session % change from Twelve Data, never fabricated.
_STRONG_GAIN_TEMPLATES = (
    "{symbol} is surging -- now ${price} ({pct})",
    "Big move up for {symbol} -- ${price} ({pct})",
    "{symbol} breaking higher, trading at ${price} ({pct})",
    "Strong session for {symbol}: ${price} ({pct})",
)
_MILD_GAIN_TEMPLATES = (
    "{symbol} ticking higher, ${price} ({pct})",
    "{symbol} inching up to ${price} ({pct})",
    "A modest gain for {symbol} -- ${price} ({pct})",
)
_FLAT_TEMPLATES = (
    "{symbol} holding steady at ${price} ({pct})",
    "Quiet session for {symbol}, ${price} ({pct})",
    "{symbol} barely moved -- ${price} ({pct})",
)
_MILD_LOSS_TEMPLATES = (
    "{symbol} slipping a bit, ${price} ({pct})",
    "{symbol} easing lower to ${price} ({pct})",
    "A soft session for {symbol} -- ${price} ({pct})",
)
_STRONG_LOSS_TEMPLATES = (
    "{symbol} sliding hard -- now ${price} ({pct})",
    "Rough session for {symbol}: ${price} ({pct})",
    "{symbol} under real pressure, ${price} ({pct})",
    "Sharp drop for {symbol}: ${price} ({pct})",
)

# session_phase -> a short tag prepended to the message, or None for the
# common case (regular session) where no extra tag is needed.
_SESSION_LABELS = {
    "premarket": "🌅 Pre-market",
    "regular": None,
    "afterhours": "🌙 After-hours",
}


def _emoji_for_change(pct):
    if pct is None:
        return "⚪"
    if pct >= 1:
        return "🟢"
    if pct <= -1:
        return "🔴"
    return "🟡"


def _scenario_templates(pct):
    if pct is None:
        return _FLAT_TEMPLATES
    if pct >= 2:
        return _STRONG_GAIN_TEMPLATES
    if pct >= 0.3:
        return _MILD_GAIN_TEMPLATES
    if pct <= -2:
        return _STRONG_LOSS_TEMPLATES
    if pct <= -0.3:
        return _MILD_LOSS_TEMPLATES
    return _FLAT_TEMPLATES


def _choose_template(templates, symbol, state):
    """Picks a template at random, excluding whichever one this symbol got
    last time (when there's more than one option) -- the whole point of a
    template bank is variety, so immediately repeating the exact same
    phrasing for the same stock next hour would defeat it."""
    last_index = state.setdefault("last_template_index", {}).get(symbol)
    choices = list(range(len(templates)))
    if last_index in choices and len(choices) > 1:
        choices.remove(last_index)
    index = random.choice(choices)
    state["last_template_index"][symbol] = index
    return templates[index]


def _session_phase(ctx):
    """US equity session phase, converted to US/Eastern (handles DST via
    zoneinfo automatically) -- 'closed' covers weekends and outside the
    4am-8pm ET pre-market/regular/after-hours window. Deliberately doesn't
    account for market holidays (same acknowledged gap as ai_manager's own
    _day_context) -- a rarer edge case than every weekend."""
    eastern_now = ctx.now.astimezone(US_EASTERN)
    if eastern_now.weekday() >= 5:  # Saturday=5, Sunday=6
        return "closed"
    t = eastern_now.time()
    if time(4, 0) <= t < time(9, 30):
        return "premarket"
    if time(9, 30) <= t < time(16, 0):
        return "regular"
    if time(16, 0) <= t < time(20, 0):
        return "afterhours"
    return "closed"


def _seasonal_note(ctx, seasonality_cfg):
    weekday_name = ctx.now.strftime("%A")
    month_name = ctx.now.strftime("%B")
    lines = []
    month_note = seasonality_cfg.get("months", {}).get(str(ctx.now.month))
    if month_note:
        lines.append(f"📅 {month_name}: {month_note}")
    weekday_note = seasonality_cfg.get("weekdays", {}).get(weekday_name)
    if weekday_note:
        lines.append(f"🗓️ {weekday_name}: {weekday_note}")
    return "\n".join(lines)


def run(ctx):
    session_phase = _session_phase(ctx)
    if session_phase == "closed":
        # No US equity session at all (weekend, or outside the 4am-8pm ET
        # window) -- skip entirely rather than repeat Friday's now-stale
        # close every hour, which is the main source of "same message"
        # duplicates this trigger could otherwise produce.
        return False

    cfg = ctx.config["thresholds"]["market_snapshot"]
    watchlist = ctx.config["watchlist"]
    symbols = cfg.get("symbols") or [
        s["symbol"] for s in (watchlist.get("stocks_broad") or watchlist.get("stocks", []))
    ]
    max_posts_per_run = cfg.get("max_posts_per_run", 2)

    # get_quotes_batch (not a per-symbol loop): its own chunking/60s-pause
    # pacing is what keeps this under Twelve Data's free-tier per-minute
    # limit at 30 symbols -- never raises, a symbol just won't be in the
    # returned dict if its chunk failed.
    quotes_by_symbol = twelvedata.get_quotes_batch(symbols)
    quotes = [
        (symbol, q["price"], q.get("percent_change"))
        for symbol, q in quotes_by_symbol.items()
        if q.get("price") is not None
    ]
    logger.info("market_snapshot_telegram: got %d/%d quotes", len(quotes), len(symbols))

    if not quotes:
        return False

    # biggest movers first (by absolute % change); a symbol with no % data
    # sorts last rather than crashing the comparison
    quotes.sort(key=lambda item: abs(item[2]) if item[2] is not None else -1, reverse=True)
    quotes = quotes[:max_posts_per_run]

    seasonal_note = _seasonal_note(ctx, ctx.config.get("seasonality", {}))
    session_label = _SESSION_LABELS.get(session_phase)
    state = ctx.state.setdefault("market_snapshot_telegram", {"last_template_index": {}})

    fired = False
    for symbol, price, pct_change in quotes:
        emoji = _emoji_for_change(pct_change)
        template = _choose_template(_scenario_templates(pct_change), symbol, state)
        body = template.format(symbol=symbol, price=fmt_price(price), pct=fmt_pct(pct_change))
        prefix = f"{session_label}: " if session_label else ""
        lines = [f"{emoji} {prefix}{body}"]
        if seasonal_note:
            lines.append(seasonal_note)
        text = "\n\n".join(lines)
        if telegram_client.send_channel_message(text):
            fired = True

    return fired
