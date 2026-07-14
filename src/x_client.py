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

from src import ops_alerts

logger = logging.getLogger("tickerwatch.x_client")

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


class XClient:
    def __init__(self):
        self.client = None
        self.api_v1 = None
        if not DRY_RUN:
            import tweepy

            creds = dict(
                consumer_key=os.environ["X_API_KEY"],
                consumer_secret=os.environ["X_API_SECRET"],
                access_token=os.environ["X_ACCESS_TOKEN"],
                access_token_secret=os.environ["X_ACCESS_SECRET"],
            )
            self.client = tweepy.Client(**creds)
            # Media upload has no v2 endpoint in tweepy yet, so it goes through
            # the older v1.1 API -- same OAuth1 credentials, no new secrets needed.
            auth = tweepy.OAuth1UserHandler(
                creds["consumer_key"], creds["consumer_secret"],
                creds["access_token"], creds["access_token_secret"],
            )
            self.api_v1 = tweepy.API(auth)

    def upload_media(self, image_bytes):
        """Uploads raw image bytes to X, returns a media_id_string for use in
        post()'s media_id, or None on any failure (never blocks the post)."""
        if DRY_RUN or not image_bytes:
            return None
        import io

        try:
            media = self.api_v1.media_upload(filename="coin.png", file=io.BytesIO(image_bytes))
            return media.media_id_string
        except Exception:
            logger.exception("Failed to upload media")
            return None

    def post(self, text, poll_options=None, poll_duration_minutes=None, media_id=None):
        if DRY_RUN:
            logger.info("[DRY RUN] would post (media_id=%s):\n%s", media_id, text)
            return "dryrun-tweet-id"
        try:
            kwargs = {"text": text}
            if poll_options:
                kwargs["poll_options"] = poll_options
                kwargs["poll_duration_minutes"] = poll_duration_minutes or 1440
            if media_id:
                kwargs["media_ids"] = [media_id]
            resp = self.client.create_tweet(**kwargs)
            tweet_id = str(resp.data["id"])
            logger.info("Posted tweet %s: https://x.com/i/web/status/%s\n%s", tweet_id, tweet_id, text)
            return tweet_id
        except Exception as e:
            logger.exception("Failed to post tweet")
            ops_alerts.notify_x_failure(f"post: {e}")
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
        except Exception as e:
            logger.exception("Failed to post reply")
            ops_alerts.notify_x_failure(f"reply: {e}")
            return None

    def retweet(self, tweet_id):
        if DRY_RUN:
            logger.info("[DRY RUN] would retweet %s", tweet_id)
            return True
        try:
            self.client.retweet(tweet_id)
            return True
        except Exception as e:
            logger.exception("Failed to retweet %s", tweet_id)
            ops_alerts.notify_x_failure(f"retweet: {e}")
            return False

    def get_user_id(self, handle):
        """Resolves an @handle to its numeric user ID (one read call). Used
        to auto-resolve config/reply_targets.json entries that only have a
        handle, so a blank user_id doesn't require a manual lookup step --
        callers should cache the result (e.g. in state) since looking it up
        every run would burn extra read budget for no reason."""
        if DRY_RUN:
            logger.info("[DRY RUN] would resolve user id for @%s", handle)
            return None
        try:
            # tweepy defaults get_user() to user_auth=False (OAuth 2.0 App-only
            # Bearer auth), which we never configure (no bearer_token) --
            # explicit user_auth=True is required to use our OAuth 1.0a
            # credentials, the same ones that already work fine for posting.
            resp = self.client.get_user(username=handle.lstrip("@"), user_auth=True)
            if not resp.data:
                return None
            return str(resp.data.id)
        except Exception:
            logger.exception("Failed to resolve user id for @%s", handle)
            return None

    def get_recent_tweet_ids(self, user_id, since_id=None, max_results=5):
        """Newest-first list of str tweet IDs posted by user_id since since_id
        (excludes retweets/replies so we never retweet a retweet or a reply).
        This is a read call, billed separately/lightly from writes and not
        tracked by Budget -- see README for the read-cost caveat."""
        if DRY_RUN:
            logger.info("[DRY RUN] would fetch tweets for user %s since %s", user_id, since_id)
            return []
        try:
            # see get_user_id's comment -- get_users_tweets also defaults to
            # user_auth=False (App-only Bearer auth) unless told otherwise.
            resp = self.client.get_users_tweets(
                id=user_id,
                since_id=since_id,
                max_results=max_results,
                exclude=["retweets", "replies"],
                user_auth=True,
            )
            if not resp.data:
                return []
            return [str(t.id) for t in resp.data]
        except Exception:
            logger.exception("Failed to fetch recent tweets for user %s", user_id)
            return []

    def get_recent_tweets_with_text(self, user_id, since_id=None, max_results=5):
        """Same as get_recent_tweet_ids but also returns each tweet's text,
        for the comment-engagement pipeline which needs the source content to
        write a relevant reply. Newest-first list of {"id": str, "text": str}."""
        if DRY_RUN:
            logger.info("[DRY RUN] would fetch tweets+text for user %s since %s", user_id, since_id)
            return []
        try:
            resp = self.client.get_users_tweets(
                id=user_id,
                since_id=since_id,
                max_results=max_results,
                exclude=["retweets", "replies"],
                user_auth=True,
            )
            if not resp.data:
                return []
            return [{"id": str(t.id), "text": t.text} for t in resp.data]
        except Exception:
            logger.exception("Failed to fetch recent tweets+text for user %s", user_id)
            return []
