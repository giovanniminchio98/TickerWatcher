"""Telegram-only (bot chat) digest of the biggest recent candidate posts to
manually reply to -- a stopgap while X API replies stay blocked by the
anti-spam/reputation gate (see README's reply-audience note). Reuses
config/reply_targets.json's account pool, the same candidates AI Manager
itself considers, ranked by real engagement (likes + retweets) so the posts
most worth jumping into surface first. Never posts to X, never costs
anything, never counts toward anything_fired -- same "Telegram-only side
channel" pattern as content_drafts.py.

A tweet is shown at most once (tracked in state, capped like every other
tweet-id list in this codebase) and is skipped if AI Manager already
replied to or reposted it -- no point suggesting a manual reply to
something already acted on.
"""
import logging

from src import telegram_client

logger = logging.getLogger("tickerwatch.triggers.reply_suggestions")


def _candidates(ctx, state):
    acted_ids = set(ctx.state.get("ai_manager", {}).get("replied_tweet_ids", [])) | set(
        ctx.state.get("ai_manager", {}).get("reposted_tweet_ids", [])
    )
    shown_ids = set(state.get("shown_tweet_ids", []))

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
            candidates.append({"handle": handle, **t})
    return candidates


def run(ctx):
    cfg = ctx.config.get("reply_suggestions", {})
    if not cfg.get("enabled", True):
        return False

    state = ctx.state["reply_suggestions"]
    candidates = _candidates(ctx, state)
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
