"""
Hard monthly safety net against the X API bill (pay-per-use pricing) or the
legacy free-tier post cap. Every actual post/retweet/reply/poll goes through
Budget.try_spend() first. The trigger pipeline runs in strict priority order
(whale -> news -> price -> daily -> flashback -> polls -> self-reply -> retweets),
so once the cap is hit for the month, only the lowest-priority post types get
skipped -- whale and news alerts are protected until the very end.

record_spend() also fires a Telegram notification (if configured): a short
budget-progress confirmation to the private cost-tracking chat (never the
bot chat -- that one's for operational messages, not dollar figures), and a
full copy of the post to the Telegram channel (channel_text if given, else
text) -- Telegram is free, so the channel copy can be more generous than the
X post itself (e.g. restoring a link X's post dropped for cost/reach reasons).

The channel text is always HTML-escaped here (centrally, once) before
sending, since Telegram messages go out with parse_mode=HTML -- callers
just pass plain text (news paraphrases, LLM-written replies, filler lines
can all contain a stray "&", e.g. "Fear & Greed", "S&P 500") and never need
to think about escaping themselves. A caller that wants a short tappable
link (instead of Telegram's giant auto-expanded preview card) passes
channel_link=(label, url) and this appends it as a proper anchor tag after
escaping the base text -- the anchor itself is never escaped.

Plus a one-time "low budget" alert (with a link to top up) the first time
usage crosses LOW_BUDGET_THRESHOLD in a given month.
"""
from datetime import datetime, timezone

from src import telegram_client

LOW_BUDGET_THRESHOLD = 0.9
X_CONSOLE_URL = "https://console.x.com/"


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

    def record_spend(self, has_link=False, text=None, channel_text=None, channel_link=None, mirror_to_channel=True):
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
        telegram_client.send_cost_message(f"✅ X post created — {progress}")
        if mirror_to_channel:
            base = channel_text if channel_text is not None else text or "(no text)"
            escaped = telegram_client.escape_html(base)
            if channel_link:
                label, url = channel_link
                escaped = f"{escaped}\n🔗 {telegram_client.link_html(label, url)}"
            telegram_client.send_channel_message(escaped)
        self._maybe_send_low_budget_alert()

    def _maybe_send_low_budget_alert(self):
        b = self.state["budget"]
        cfg = self.config
        if cfg["mode"] == "posts":
            cap, used = cfg["monthly_post_cap"], b["posts_used"]
        else:
            cap, used = cfg["monthly_usd_cap"], b["usd_used"]
        if not cap or used / cap < LOW_BUDGET_THRESHOLD:
            return
        if b.get("low_budget_alert_sent_period") == b["period"]:
            return  # already alerted this month

        pct = used / cap * 100
        if cfg["mode"] == "posts":
            text = f"⚠️ TickerWatch budget alert: {used}/{cap} posts used ({pct:.0f}%) this month."
        else:
            text = (
                f"⚠️ TickerWatch budget alert: ${used:.2f}/${cap:.2f} used ({pct:.0f}%) this month.\n"
                f"Add credits: {X_CONSOLE_URL} (Billing -> Credits)"
            )
        telegram_client.send_cost_message(text)
        b["low_budget_alert_sent_period"] = b["period"]

    def remaining_summary(self):
        b = self.state["budget"]
        cfg = self.config
        if cfg["mode"] == "posts":
            return f"{b['posts_used']}/{cfg['monthly_post_cap']} posts used this period"
        return f"${b['usd_used']:.2f}/${cfg['monthly_usd_cap']:.2f} used this period"
