"""Post type 1 (highest priority): whale/on-chain alerts. See sources/whale_btc.py
and sources/whale_eth.py for the free-tier data sources and their trade-offs.

The main post has no tx-explorer link: at X's pay-per-use pricing, any post
containing a URL jumps from $0.015 to $0.20. Instead, a cheap follow-up reply
(plain text, not a clickable link, so it stays at $0.015) carries the raw tx
reference -- still real, verifiable, on-chain data, just not click-to-verify
without pasting it into an explorer yourself. Siren count scales with size
(more sirens = bigger transaction) so the visual weight matches the news."""
import logging
import math

from src.formatting import fmt_usd_compact, truncate
from src.sources import whale_btc, whale_eth

logger = logging.getLogger("tickerwatch.triggers.whale")

SIREN_UNIT_USD = 20_000_000  # one siren per $20M, capped at 10 (reached at $200M+)
MAX_SIRENS = 10


def _siren_count(usd):
    if not usd:
        return "🚨"
    count = max(1, min(MAX_SIRENS, math.ceil(usd / SIREN_UNIT_USD)))
    return "🚨" * count


def _post_and_reply_with_ref(ctx, text, ref_label, ref_value):
    tweet_id = ctx.x.post(text)
    if not tweet_id:
        return False
    ctx.budget.record_spend(has_link=False, text=text)
    if ctx.budget.can_spend(has_link=False):
        reply_text = truncate(f"{ref_label}: {ref_value}")
        reply_id = ctx.x.reply(reply_text, tweet_id)
        if reply_id:
            ctx.budget.record_spend(has_link=False, text=reply_text)
    return True


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
        sirens = _siren_count(hit["usd"])
        usd_part = f" ({fmt_usd_compact(hit['usd'])})" if hit["usd"] else ""
        text = truncate(
            f"{sirens} WHALE ALERT\n{hit['btc']:.1f} BTC{usd_part} just moved on-chain\n#BTC #Crypto"
        )
        if _post_and_reply_with_ref(ctx, text, "tx", hit["txid"]):
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
        sirens = _siren_count(hit["usd"])
        text = truncate(
            f"{sirens} WHALE ALERT\n{hit['eth']:.1f} ETH ({fmt_usd_compact(hit['usd'])}) just moved on-chain\n#ETH #Crypto"
        )
        if _post_and_reply_with_ref(ctx, text, "tx", hit["txhash"]):
            posted += 1
            fired = True
    return fired


def run(ctx):
    btc_fired = _post_btc_alerts(ctx)
    eth_fired = _post_eth_alerts(ctx)
    return btc_fired or eth_fired
