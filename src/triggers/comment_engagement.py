"""Post type 9 (opt-in, deliberately separate from the safe-by-construction
pipeline): scheduled comments/replies under specific OTHER accounts' latest
posts. Everywhere else in TickerWatch, "never auto-reply under other
accounts' posts" was a hard rule -- this trigger is the one deliberate,
explicitly-approved exception, so it's kept on a short leash:

- Nothing fires for an account unless it's both enabled=true AND has a
  resolved user_id in config/reply_targets.json (same convention as
  config/accounts.json's retweet list) -- so an empty/placeholder entry is
  inert by construction, not just by a flag.
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
        if not target.get("enabled") or not target.get("user_id"):
            continue
        handle = target["handle"]
        cap = max(0, target.get("times_per_day", 1))

        acct_state = state.setdefault(
            handle, {"date": None, "commented_today": 0, "last_seen_tweet_id": None}
        )
        if acct_state["date"] != today_str:
            acct_state["date"] = today_str
            acct_state["commented_today"] = 0

        if acct_state["commented_today"] >= cap:
            continue

        tweets = ctx.x.get_recent_tweets_with_text(target["user_id"], since_id=acct_state["last_seen_tweet_id"])
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
