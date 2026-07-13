"""Retweet pipeline -- separate from the posting pipeline per the hard
constraint: retweet only, NEVER auto-reply/auto-comment under other accounts'
tweets. Reads (checking each account's timeline) are billed separately/lightly
by X and deduped within a 24h window; only the retweet action itself (a write)
is tracked against Budget."""
import logging

logger = logging.getLogger("tickerwatch.triggers.retweets")


def run(ctx):
    accounts = ctx.config["accounts"]["monitored_accounts"]
    state = ctx.state["retweets"]
    count = 0

    for account in accounts:
        if not account.get("enabled") or not account.get("user_id"):
            continue
        handle = account["handle"]
        since_id = state["last_seen_tweet_id"].get(handle)
        new_ids = ctx.x.get_recent_tweet_ids(account["user_id"], since_id=since_id)
        if not new_ids:
            continue

        for tweet_id in new_ids:  # newest-first
            if not ctx.budget.can_spend(has_link=False):
                break
            if ctx.x.retweet(tweet_id):
                ctx.budget.record_spend(has_link=False, text=f"retweet of @{handle}'s post {tweet_id}")
                count += 1

        state["last_seen_tweet_id"][handle] = new_ids[0]

    return count
