"""Post type 9 (opt-in, deliberately separate from the safe-by-construction
pipeline): scheduled comments/replies under specific OTHER accounts' latest
posts. Everywhere else in TickerWatch, "never auto-reply under other
accounts' posts" was a hard rule -- this trigger is the one deliberate,
explicitly-approved exception, so it's kept on a short leash:

- Nothing fires for an account unless enabled=true in
  config/reply_targets.json -- an entry with enabled=false is inert by
  construction, not just skipped by a runtime check.
- user_id is optional in config: if left blank, it's auto-resolved from the
  handle on first use (one read call) and cached in state so it's never
  looked up again -- no manual "paste the numeric ID" step needed to add a
  new account. Fill it in manually only if you want to skip that first
  lookup call.
- times_per_day is a hard per-account cap, checked before every reply.
- The reply text is always freshly written by Claude from the target
  tweet's own content (src/sources/reply_writer.py), never a canned line --
  if that fails or ANTHROPIC_API_KEY isn't set, the slot is skipped rather
  than posting something generic/bot-sounding.
"""
import logging

from src.formatting import truncate
from src.sources import reply_writer

logger = logging.getLogger("tickerwatch.triggers.comment_engagement")


def run(ctx):
    targets = ctx.config["reply_targets"]["targets"]
    state = ctx.state["comment_engagement"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    fired = False

    for target in targets:
        if not target.get("enabled"):
            continue
        handle = target["handle"]
        cap = max(0, target.get("times_per_day", 1))

        acct_state = state.setdefault(
            handle,
            {"date": None, "commented_today": 0, "last_seen_tweet_id": None, "resolved_user_id": None},
        )
        if acct_state["date"] != today_str:
            acct_state["date"] = today_str
            acct_state["commented_today"] = 0

        if acct_state["commented_today"] >= cap:
            continue

        user_id = target.get("user_id") or acct_state.get("resolved_user_id")
        if not user_id:
            user_id = ctx.x.get_user_id(handle)
            if not user_id:
                continue
            acct_state["resolved_user_id"] = user_id

        tweets = ctx.x.get_recent_tweets_with_text(user_id, since_id=acct_state["last_seen_tweet_id"])
        if not tweets:
            continue

        for tweet in reversed(tweets):  # oldest-first so replies land in chronological order
            if acct_state["commented_today"] >= cap:
                break
            if not ctx.budget.can_spend(has_link=False):
                break
            try:
                comment = reply_writer.write_reply(tweet["text"])
            except Exception:
                logger.exception("Reply generation failed for @%s tweet %s", handle, tweet["id"])
                continue
            if not comment:
                continue
            reply_id = ctx.x.reply(truncate(comment), tweet["id"])
            if reply_id:
                ctx.budget.record_spend(has_link=False, text=f"Reply to @{handle}: {comment}")
                acct_state["commented_today"] += 1
                fired = True

        acct_state["last_seen_tweet_id"] = tweets[0]["id"]

    return fired
