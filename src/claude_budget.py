"""
Hard monthly safety net against the Claude API bill, mirroring src/budget.py's
shape but for a different pricing model: every Claude call is billed on real
input/output token usage rather than a flat per-post rate, so cost is only
knowable after the call returns. record_spend() is therefore called AFTER
every ai_manager Claude call (regardless of what was decided -- a call was
still made and billed), computed from the response's actual usage counts
against MODEL_PRICING, never estimated up front.

Deliberately a second, independent budget object from Budget (X API spend):
config/budget.json's monthly_usd_cap and config/claude_budget.json's
monthly_usd_cap are sized so their sum is the account-wide monthly ceiling
(see README) -- each one stopping independently at its own hard cap is what
makes that combined ceiling a structural guarantee rather than an estimate.
"""
from datetime import datetime, timezone

from src import telegram_client

LOW_BUDGET_THRESHOLD = 0.9

# $ per 1M tokens (input, output). Update if Anthropic's published pricing
# changes -- see the claude-api skill for the current table.
MODEL_PRICING = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-8": (5.00, 25.00),
}


def _current_period():
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


class ClaudeBudget:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self._roll_period_if_needed()

    def _roll_period_if_needed(self):
        b = self.state["claude_budget"]
        period = _current_period()
        if b.get("period") != period:
            b["period"] = period
            b["usd_used"] = 0.0

    def can_spend(self):
        b = self.state["claude_budget"]
        return b["usd_used"] < self.config["monthly_usd_cap"]

    def record_spend(self, usage, model):
        """usage is the response's .usage object (input_tokens/output_tokens
        attrs). Always call this after a Claude call completes, regardless of
        what the call decided -- the tokens were spent either way."""
        b = self.state["claude_budget"]
        cfg = self.config
        input_per_1m, output_per_1m = MODEL_PRICING.get(model, MODEL_PRICING["claude-sonnet-5"])
        cost = (usage.input_tokens / 1_000_000 * input_per_1m) + (usage.output_tokens / 1_000_000 * output_per_1m)
        b["usd_used"] = round(b["usd_used"] + cost, 4)
        self._maybe_send_low_budget_alert()
        return cost

    def _maybe_send_low_budget_alert(self):
        b = self.state["claude_budget"]
        cfg = self.config
        cap = cfg["monthly_usd_cap"]
        if not cap or b["usd_used"] / cap < LOW_BUDGET_THRESHOLD:
            return
        if b.get("low_budget_alert_sent_period") == b["period"]:
            return
        pct = b["usd_used"] / cap * 100
        telegram_client.send_message(
            f"⚠️ TickerWatch Claude API budget alert: ${b['usd_used']:.2f}/${cap:.2f} used ({pct:.0f}%) this month."
        )
        b["low_budget_alert_sent_period"] = b["period"]

    def remaining_summary(self):
        b = self.state["claude_budget"]
        cfg = self.config
        return f"${b['usd_used']:.2f}/${cfg['monthly_usd_cap']:.2f} used this period (Claude API)"
