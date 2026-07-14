"""Shared per-run objects passed into every trigger function."""
from datetime import datetime, timezone


class Context:
    def __init__(self, config, state, budget, x_client, prices, claude_budget=None):
        self.config = config
        self.state = state
        self.budget = budget
        self.claude_budget = claude_budget
        self.x = x_client
        self.prices = prices  # {coingecko_id: {"usd": float, "usd_24h_change": float}}
        self.now = datetime.now(timezone.utc)

    def register_self_reply_candidate(self, symbol, price, tweet_id):
        self.state["self_reply"]["pending"].append(
            {
                "symbol": symbol,
                "price": price,
                "tweet_id": tweet_id,
                "posted_at": self.now.timestamp(),
                "replied": False,
            }
        )
