"""Post type 5 (lowest priority filler): historical flashback, max once/day,
only posted when nothing higher-priority fired this run. Crypto-only for now
since CoinGecko's free /coins/{id}/history endpoint makes this simple and free;
stocks could be added via Twelve Data's time_series endpoint if desired."""
import logging
import random
from datetime import timedelta

from src.formatting import dot_for_change, fmt_pct, fmt_price, truncate
from src.sources import coingecko

logger = logging.getLogger("tickerwatch.triggers.flashback")


def run(ctx, higher_priority_fired):
    state = ctx.state["flashback"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    if higher_priority_fired or state["last_posted_date"] == today_str:
        return False

    cfg = ctx.config["thresholds"]["flashback"]
    crypto_list = list(ctx.config["watchlist"]["crypto"])
    random.shuffle(crypto_list)

    for asset in crypto_list:
        price_now = ctx.prices.get(asset["coingecko_id"], {}).get("usd")
        if not price_now:
            continue
        for years in cfg["years_back_options"]:
            date_then = ctx.now - timedelta(days=365 * years)
            try:
                price_then = coingecko.get_price_on_date(asset["coingecko_id"], date_then.strftime("%d-%m-%Y"))
            except Exception:
                logger.exception("CoinGecko history lookup failed for %s", asset["symbol"])
                continue
            if not price_then:
                continue

            if not ctx.budget.can_spend(has_link=False):
                return False

            change = (price_now - price_then) / price_then * 100
            text = truncate(
                f"📅 On this day in {date_then.year}, ${asset['symbol']} was trading at ${fmt_price(price_then)}\n"
                f"Today: ${fmt_price(price_now)}\n{dot_for_change(change)} Change: {fmt_pct(change)}"
            )
            tweet_id = ctx.x.post(text)
            if tweet_id:
                ctx.budget.record_spend(has_link=False, text=text)
                state["last_posted_date"] = today_str
                return True
    return False
