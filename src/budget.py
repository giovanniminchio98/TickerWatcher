"""
Hard monthly safety net against the X API bill (pay-per-use pricing) or the
legacy free-tier post cap. Every actual post/retweet/reply/poll goes through
Budget.try_spend() first. The trigger pipeline runs in strict priority order
(whale -> news -> price -> daily -> flashback -> polls -> self-reply -> retweets),
so once the cap is hit for the month, only the lowest-priority post types get
skipped -- whale and news alerts are protected until the very end.

record_spend() also fires a per-post Telegram notification (if configured)
so every post is confirmed in near-real-time along with running budget usage.
"""
from datetime import datetime, timezone

from src import telegram_client


def _current_period():
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _current_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Budget:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self._roll_period_if_needed()
        self._roll_day_if_needed()

    def _roll_period_if_needed(self):
        b = self.state["budget"]
        period = _current_period()
        if b.get("period") != period:
            b["period"] = period
            b["posts_used"] = 0
            b["usd_used"] = 0.0

    def _roll_day_if_needed(self):
        b = self.state["budget"]
        day = _current_day()
        daily = b.setdefault("daily", {"date": None, "posts_used": 0, "usd_used": 0.0})
        if daily.get("date") != day:
            daily["date"] = day
            daily["posts_used"] = 0
            daily["usd_used"] = 0.0

    def _would_exceed(self, has_link):
        b = self.state["budget"]
        cfg = self.config
        if cfg["mode"] == "posts":
            return (b["posts_used"] + 1) > cfg["monthly_post_cap"]
        cost = cfg["cost_per_post_with_link_usd"] if has_link else cfg["cost_per_post_usd"]
        return (b["usd_used"] + cost) > cfg["monthly_usd_cap"]

    def can_spend(self, has_link=False):
        return not self._would_exceed(has_link)

    def record_spend(self, has_link=False, text=None):
        b = self.state["budget"]
        cfg = self.config
        cost = cfg["cost_per_post_with_link_usd"] if has_link else cfg["cost_per_post_usd"]
        b["posts_used"] += 1
        b["usd_used"] = round(b["usd_used"] + cost, 4)
        b["daily"]["posts_used"] += 1
        b["daily"]["usd_used"] = round(b["daily"]["usd_used"] + cost, 4)

        if cfg["mode"] == "posts":
            progress = f"{b['posts_used']}/{cfg['monthly_post_cap']} posts"
        else:
            progress = f"${b['usd_used']:.2f}/${cfg['monthly_usd_cap']:.2f}"
        telegram_client.send_message(f"X post created: {text or '(no text)'}\n{progress}")

    def remaining_summary(self):
        b = self.state["budget"]
        cfg = self.config
        if cfg["mode"] == "posts":
            return f"{b['posts_used']}/{cfg['monthly_post_cap']} posts used this period"
        return f"${b['usd_used']:.2f}/${cfg['monthly_usd_cap']:.2f} used this period"
