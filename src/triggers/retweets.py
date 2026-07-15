"""Retweet pipeline -- separate from the posting pipeline per the hard
constraint: retweet only, NEVER auto-reply/auto-comment under other accounts'
tweets. Reads (checking each account's timeline) are billed separately/lightly
by X and deduped within a 24h window; only the retweet action itself (a write)
is tracked against Budget.

Retweeting doesn't carry the same anti-spam engagement threshold that API
replies do (confirmed live -- replies to well-established accounts get a 403
"not mentioned or otherwise engaged" until the account builds up history, but
a plain retweet has no such reply-audience check), so this is a lower-risk
engagement lever to lean on while that trust builds up.

'user_id' is optional in config: leave it blank and it auto-resolves from
'handle' on first use (one read call, then cached in state), same pattern as
comment_engagement.py and ai_manager.py -- no manual ID lookup needed."""
import logging

logger = logging.getLogger("tickerwatch.triggers.retweets")


def run(ctx):
    accounts = ctx.config["accounts"]["monitored_accounts"]
    state = ctx.state["retweets"]
    count = 0

    for account in accounts:
        if not account.get("enabled"):
            continue
        handle = account["handle"]
        resolved = state.setdefault("resolved_accounts", {}).setdefault(handle, {})
        user_id = account.get("user_id") or resolved.get("resolved_user_id")
        if not user_id:
            user_id = ctx.x.get_user_id(handle)
            if not user_id:
                continue
            resolved["resolved_user_id"] = user_id
        since_id = state["last_seen_tweet_id"].get(handle)
        new_ids = ctx.x.get_recent_tweet_ids(user_id, since_id=since_id)
        if not new_ids:
            continue

        # Only ever the single newest tweet per account per run -- never
        # backfill a multi-tweet backlog (e.g. a prolific account's last 5
        # posts on the very first run), which would spike write volume
        # right when we're trying to keep activity low-key after today's
        # test burst.
        tweet_id = new_ids[0]
        if ctx.budget.can_spend(has_link=False) and ctx.x.retweet(tweet_id):
            ctx.budget.record_spend(has_link=False, text=f"retweet of @{handle}'s post {tweet_id}")
            count += 1

        state["last_seen_tweet_id"][handle] = new_ids[0]

    return count
