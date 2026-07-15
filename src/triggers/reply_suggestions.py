"""Telegram-only (bot chat) digest of the biggest recent candidate posts to
manually reply to -- covers every account in config/reply_targets.json
(big and small reply_only ones alike). Confirmed live: X's "you must be
mentioned or otherwise engaged by the author" reply restriction isn't a
per-account setting -- it hit the smaller reply_only accounts just as hard
as the bigger ones, so reply_manager.py (automated AI replies) is disabled
entirely and this manual digest is now the only reply path, for every
account, not just the bigger ones. Ranked by real engagement (likes +
retweets) so the posts most worth jumping into surface first. Never posts
to X, never costs anything, never counts toward anything_fired -- same
"Telegram-only side channel" pattern as content_drafts.py.

Candidates older than max_age_hours (config, default 6) are dropped before
ranking -- replying to a post from many hours ago reads as stale and the
post has usually already peaked, so raw engagement alone isn't enough of a
signal; recency matters too.

A tweet is shown at most once (tracked in state, capped like every other
tweet-id list in this codebase) and is skipped if AI Manager already
reposted it -- no point suggesting a manual reply to something already
acted on.
"""
import logging
from datetime import timedelta

from src import telegram_client

logger = logging.getLogger("tickerwatch.triggers.reply_suggestions")


def _candidates(ctx, state, max_age_hours):
    acted_ids = set(ctx.state.get("ai_manager", {}).get("reposted_tweet_ids", []))
    shown_ids = set(state.get("shown_tweet_ids", []))
    cutoff = ctx.now - timedelta(hours=max_age_hours)

    candidates = []
    for target in ctx.config["reply_targets"]["targets"]:
        if not target.get("enabled"):
            continue
        handle = target["handle"]
        acct_state = state.setdefault("resolved_accounts", {}).setdefault(handle, {})
        user_id = target.get("user_id") or acct_state.get("resolved_user_id")
        if not user_id:
            user_id = ctx.x.get_user_id(handle)
            if not user_id:
                continue
            acct_state["resolved_user_id"] = user_id

        tweets = ctx.x.get_recent_tweets_with_metrics(user_id, max_results=5)
        for t in tweets:
            if t["id"] in shown_ids or t["id"] in acted_ids:
                continue
            if t.get("created_at") and t["created_at"] < cutoff:
                continue
            candidates.append({"handle": handle, **t})
    return candidates


def run(ctx):
    cfg = ctx.config.get("reply_suggestions", {})
    if not cfg.get("enabled", True):
        return False

    state = ctx.state["reply_suggestions"]
    candidates = _candidates(ctx, state, cfg.get("max_age_hours", 6))
    if not candidates:
        return False

    candidates.sort(key=lambda c: c["like_count"] + c["retweet_count"], reverse=True)
    top = candidates[: cfg.get("max_per_run", 3)]

    lines = ["💬 Reply candidates (biggest right now, tap to open + comment):"]
    for c in top:
        snippet = c["text"][:180]
        link = f"https://x.com/{c['handle']}/status/{c['id']}"
        lines.append(
            f"\n@{c['handle']} ({c['like_count']} likes / {c['retweet_count']} RTs): {snippet}\n{link}"
        )
    telegram_client.send_message("\n".join(lines))

    state["shown_tweet_ids"] = (state.get("shown_tweet_ids", []) + [c["id"] for c in top])[-500:]
    return True
