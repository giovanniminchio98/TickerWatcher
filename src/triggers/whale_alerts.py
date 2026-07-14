"""Post type 1 (highest priority): whale/on-chain alerts. See sources/whale_btc.py
and sources/whale_eth.py for the free-tier data sources and their trade-offs.

The tx reference is a plain-text hash on its own line in the same post (not a
clickable link), so it stays real and verifiable without pasting it into an
explorer -- and since it's not a URL, it costs nothing extra either way,
so it's just part of the one $0.015 post instead of a separate reply. (Unlike
a real link, a raw hash doesn't trigger X's algorithmic reach suppression, so
there was no reach reason to split it out -- only cost, and merging into one
post actually halves the per-alert cost vs. a separate reply.)

The asset symbol uses a $ cashtag ($BTC/$ETH) rather than plain text --
confirmed via a live billing test that this does NOT trigger the $0.20
"post contains a link" surcharge (X's Smart Cashtags are a distinct,
in-app-only entity type, never an external URL).

Siren count scales with size (more sirens = bigger transaction, capped at
10 for $200M+) so the visual weight matches the news."""
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


def _post_with_ref(ctx, text, ref_value):
    full_text = truncate(f"{text}\n\n{ref_value}")
    tweet_id = ctx.x.post(full_text)
    if not tweet_id:
        return False
    ctx.budget.record_spend(has_link=False, text=full_text)
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
        text = f"{sirens} WHALE ALERT\n{hit['btc']:.1f} $BTC{usd_part} just moved on-chain\n#BTC #Crypto"
        if _post_with_ref(ctx, text, hit["txid"]):
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
        text = f"{sirens} WHALE ALERT\n{hit['eth']:.1f} $ETH ({fmt_usd_compact(hit['usd'])}) just moved on-chain\n#ETH #Crypto"
        if _post_with_ref(ctx, text, hit["txhash"]):
            posted += 1
            fired = True
    return fired


def run(ctx):
    btc_fired = _post_btc_alerts(ctx)
    eth_fired = _post_eth_alerts(ctx)
    return btc_fired or eth_fired
