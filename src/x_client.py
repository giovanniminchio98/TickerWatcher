"""
Thin wrapper around tweepy.Client (X API v2, OAuth 1.0a user context).
Every write method returns the created tweet ID (str) on success, or None on
failure/dry-run -- callers must treat None as "did not post" and not touch
dedup state for that item.

Set DRY_RUN=1 in the environment to log what *would* be posted without ever
calling the X API. Always test with DRY_RUN=1 first (see README).
"""
import logging
import os

logger = logging.getLogger("tickerwatch.x_client")

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


class XClient:
    def __init__(self):
        self.client = None
        if not DRY_RUN:
            import tweepy

            self.client = tweepy.Client(
                consumer_key=os.environ["X_API_KEY"],
                consumer_secret=os.environ["X_API_SECRET"],
                access_token=os.environ["X_ACCESS_TOKEN"],
                access_token_secret=os.environ["X_ACCESS_SECRET"],
            )

    def post(self, text, poll_options=None, poll_duration_minutes=None):
        if DRY_RUN:
            logger.info("[DRY RUN] would post:\n%s", text)
            return "dryrun-tweet-id"
        try:
            kwargs = {"text": text}
            if poll_options:
                kwargs["poll_options"] = poll_options
                kwargs["poll_duration_minutes"] = poll_duration_minutes or 1440
            resp = self.client.create_tweet(**kwargs)
            tweet_id = str(resp.data["id"])
            logger.info("Posted tweet %s: https://x.com/i/web/status/%s\n%s", tweet_id, tweet_id, text)
            return tweet_id
        except Exception:
            logger.exception("Failed to post tweet")
            return None

    def reply(self, text, in_reply_to_tweet_id):
        if DRY_RUN:
            logger.info("[DRY RUN] would reply to %s:\n%s", in_reply_to_tweet_id, text)
            return "dryrun-reply-id"
        try:
            resp = self.client.create_tweet(text=text, in_reply_to_tweet_id=in_reply_to_tweet_id)
            reply_id = str(resp.data["id"])
            logger.info("Posted reply %s to %s: https://x.com/i/web/status/%s\n%s", reply_id, in_reply_to_tweet_id, reply_id, text)
            return reply_id
        except Exception:
            logger.exception("Failed to post reply")
            return None

    def retweet(self, tweet_id):
        if DRY_RUN:
            logger.info("[DRY RUN] would retweet %s", tweet_id)
            return True
        try:
            self.client.retweet(tweet_id)
            return True
        except Exception:
            logger.exception("Failed to retweet %s", tweet_id)
            return False

    def get_recent_tweet_ids(self, user_id, since_id=None, max_results=5):
        """Newest-first list of str tweet IDs posted by user_id since since_id
        (excludes retweets/replies so we never retweet a retweet or a reply).
        This is a read call, billed separately/lightly from writes and not
        tracked by Budget -- see README for the read-cost caveat."""
        if DRY_RUN:
            logger.info("[DRY RUN] would fetch tweets for user %s since %s", user_id, since_id)
            return []
        try:
            resp = self.client.get_users_tweets(
                id=user_id,
                since_id=since_id,
                max_results=max_results,
                exclude=["retweets", "replies"],
            )
            if not resp.data:
                return []
            return [str(t.id) for t in resp.data]
        except Exception:
            logger.exception("Failed to fetch recent tweets for user %s", user_id)
            return []
