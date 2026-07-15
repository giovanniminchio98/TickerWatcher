"""
Hard monthly safety net against the image-generation API bill (DALL-E via
OpenAI, opt-in via OPENAI_API_KEY presence), mirroring claude_budget.py's
shape: a third, fully independent budget from Budget (X API) and
ClaudeBudget (Claude API) -- this one only exists to bound whatever a
separate image-generation provider costs, since that's not part of the
original $50/month X+Claude structural ceiling (see README). Sized
separately and tracked separately on purpose, so a runaway image bill can
never eat into the X or Claude caps.

record_spend() is called with the real per-image cost (a flat rate looked
up by size/quality from IMAGE_PRICING, since OpenAI's Images API doesn't
return per-call token usage the way Claude's does) after every actual
generation call, regardless of whether the resulting image ends up used.
"""
from datetime import datetime, timezone

from src import telegram_client

LOW_BUDGET_THRESHOLD = 0.9

# $ per image, by (model, size, quality). Update if OpenAI's published
# pricing changes.
IMAGE_PRICING = {
    ("dall-e-3", "1024x1024", "standard"): 0.04,
    ("dall-e-3", "1024x1024", "hd"): 0.08,
    ("dall-e-3", "1792x1024", "standard"): 0.08,
    ("dall-e-3", "1792x1024", "hd"): 0.12,
}
DEFAULT_IMAGE_COST = 0.04


def _current_period():
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


class ImageBudget:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self._roll_period_if_needed()

    def _roll_period_if_needed(self):
        b = self.state["image_budget"]
        period = _current_period()
        if b.get("period") != period:
            b["period"] = period
            b["usd_used"] = 0.0

    def can_spend(self):
        b = self.state["image_budget"]
        return b["usd_used"] < self.config["monthly_usd_cap"]

    def record_spend(self, model="dall-e-3", size="1024x1024", quality="standard"):
        b = self.state["image_budget"]
        cost = IMAGE_PRICING.get((model, size, quality), DEFAULT_IMAGE_COST)
        b["usd_used"] = round(b["usd_used"] + cost, 4)
        self._maybe_send_low_budget_alert()
        return cost

    def _maybe_send_low_budget_alert(self):
        b = self.state["image_budget"]
        cfg = self.config
        cap = cfg["monthly_usd_cap"]
        if not cap or b["usd_used"] / cap < LOW_BUDGET_THRESHOLD:
            return
        if b.get("low_budget_alert_sent_period") == b["period"]:
            return
        pct = b["usd_used"] / cap * 100
        telegram_client.send_cost_message(
            f"⚠️ TickerWatch image-generation budget alert: ${b['usd_used']:.2f}/${cap:.2f} used ({pct:.0f}%) this month.\n"
            f"Add credits: https://platform.openai.com/settings/organization/billing"
        )
        b["low_budget_alert_sent_period"] = b["period"]

    def remaining_summary(self):
        b = self.state["image_budget"]
        cfg = self.config
        return f"${b['usd_used']:.2f}/${cfg['monthly_usd_cap']:.2f} used this period (image generation)"
