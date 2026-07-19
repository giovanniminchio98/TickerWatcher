"""
Shared, cross-trigger memory of "what has this account already posted
about recently" -- prevents two independent triggers (news_alerts.py's
mechanical keyword-matched alerts, and ai_manager.py's Claude-judged
decisions) from covering the same real-world story within hours of each
other just because neither one knows what the other already posted.

Confirmed live: the same underlying story (a Citadel Securities investment
in Crypto.com, a Visa stablecoin launch) got covered twice a few hours
apart -- once by ai_manager (Claude's own synthesis, not necessarily tied
to the exact article URL news_alerts later picked up) and once by
news_alerts (a fresh RSS hit ai_manager's own history never saw). Each
trigger only ever checked its own separate dedup list -- news_alerts'
own posted_urls (count-capped at 500, but only ever consulted by
news_alerts itself) and ai_manager's own recent_news_urls/recent_post_texts
(count-capped at 12/10, only ever consulted by ai_manager itself).

Time-windowed (not count-windowed) on purpose: at the current ~15-25
posts/day pace, a fixed item-count cap covers a wildly different span of
real time depending on how much happened that day. A rolling multi-day
window is a stable, predictable memory span regardless of volume --
sized to comfortably cover WINDOW_HOURS worth of real time either way.
"""
import time

WINDOW_HOURS = 72


def add_entry(state, text, url=None, now_ts=None):
    """Records one posted item (from any trigger) into the shared history.
    `text` is what gets shown back to Claude for semantic "is this the same
    story" judgment even when the URL differs or is absent (e.g. a
    generic/evergreen ai_manager post with no single source article)."""
    ts = now_ts if now_ts is not None else time.time()
    history = state.setdefault("story_history", [])
    history.append({"text": text, "url": url, "posted_at": ts})
    state["story_history"] = _prune(history, ts)


def _prune(history, now_ts):
    cutoff = now_ts - WINDOW_HOURS * 3600
    return [e for e in history if e.get("posted_at", 0) >= cutoff]


def recent_urls(state, now_ts):
    """Set of every URL posted (by any trigger) within the rolling window."""
    history = _prune(state.get("story_history", []), now_ts)
    return {e["url"] for e in history if e.get("url")}


def recent_texts(state, now_ts, limit=30):
    """Newest-first list of post texts within the rolling window, for
    Claude's own semantic judgment of "is this basically the same story" --
    catches same-story-different-URL cases plain URL matching can't. Capped
    at `limit` most recent to keep the prompt a reasonable size even on an
    unusually high-volume day.

    Pass limit=None for the full window with no item cap -- confirmed live
    that the same Citadel/Crypto.com $400M story got posted three times
    over 57 hours (each from a different outlet, so recent_urls' URL match
    never caught it either) because a high-volume day (oracle_alerts alone
    added 8+ posts in a few hours) pushed the earlier mentions past the
    default limit=30 well before they aged out of the 72h window --
    ai_manager.py's deterministic _is_likely_duplicate check needs the
    *whole* window, uncapped, since it's cheap local string comparison, not
    something that costs prompt tokens the way the LLM-facing snapshot
    field does. Only the prompt's own "RECENTLY POSTED" context should stay
    capped at the default -- Claude doesn't need hundreds of old posts to
    judge voice/style, just the deterministic backstop does."""
    history = _prune(state.get("story_history", []), now_ts)
    return [e["text"] for e in reversed(history)][:limit]
