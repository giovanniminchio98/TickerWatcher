"""Post type 1 (highest priority): whale/on-chain alerts. See sources/whale_btc.py
and sources/whale_eth.py for the free-tier data sources and their trade-offs.

No tx-explorer link is included in the post text: at X's pay-per-use pricing,
any post containing a URL jumps from $0.015 to $0.20, and whale alerts can
fire often enough that the link cost adds up fast. The amount is still real,
on-chain data (never fabricated) -- it's just not click-to-verify from the
tweet itself. Anyone who wants to check can look up the amount/timestamp on
a block explorer directly."""
import logging

from src.formatting import fmt_usd_compact, truncate
from src.sources import whale_btc, whale_eth

logger = logging.getLogger("tickerwatch.triggers.whale")


def _post_btc_alerts(ctx):
    state = ctx.state["whale"]
    th = ctx.config["thresholds"]["whale"]
    btc_price = ctx.prices.get("bitcoin", {}).get("usd")
    fired = False
    try:
        new_height, hits = whale_btc.find_large_transactions(
            state["last_btc_block_height"], th["btc_min_amount"], btc_price
        )
    except Exception:
        logger.exception("BTC whale scan failed")
        return False
    state["last_btc_block_height"] = new_height

    posted = 0
    seen = set(state["seen_btc_txids"])
    for hit in hits:
        if posted >= th["max_alerts_per_run"]:
            break
        if hit["txid"] in seen:
            continue
        if not ctx.budget.can_spend(has_link=False):
            break
        usd_part = f" ({fmt_usd_compact(hit['usd'])})" if hit["usd"] else ""
        text = truncate(
            f"🐋 WHALE ALERT\n{hit['btc']:.1f} BTC{usd_part} just moved on-chain\n#BTC #Crypto"
        )
        tweet_id = ctx.x.post(text)
        if tweet_id:
            ctx.budget.record_spend(has_link=False, text=text)
            state["seen_btc_txids"].append(hit["txid"])
            posted += 1
            fired = True
    state["seen_btc_txids"] = state["seen_btc_txids"][-500:]
    return fired


def _post_eth_alerts(ctx):
    state = ctx.state["whale"]
    th = ctx.config["thresholds"]["whale"]
    eth_price = ctx.prices.get("ethereum", {}).get("usd")
    fired = False
    try:
        new_block, hits = whale_eth.find_large_transactions(
            state["last_eth_block"], th["eth_min_usd"], eth_price
        )
    except Exception:
        logger.exception("ETH whale scan failed")
        return False
    state["last_eth_block"] = new_block

    posted = 0
    for hit in hits:
        if posted >= th["max_alerts_per_run"]:
            break
        if not ctx.budget.can_spend(has_link=False):
            break
        text = truncate(
            f"🐋 WHALE ALERT\n{hit['eth']:.1f} ETH ({fmt_usd_compact(hit['usd'])}) just moved on-chain\n#ETH #Crypto"
        )
        tweet_id = ctx.x.post(text)
        if tweet_id:
            ctx.budget.record_spend(has_link=False, text=text)
            posted += 1
            fired = True
    return fired


def run(ctx):
    btc_fired = _post_btc_alerts(ctx)
    eth_fired = _post_eth_alerts(ctx)
    return btc_fired or eth_fired
