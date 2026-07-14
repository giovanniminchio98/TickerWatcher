"""
Free ETH whale detection via Etherscan's free API key (the "proxy" module,
which just wraps standard Ethereum JSON-RPC and is available on the free
tier -- no Whale Alert subscription needed).

Same bounded-scan trade-off as whale_btc.py: only the most recent
MAX_BLOCKS_PER_RUN blocks are scanned per run to stay well within Etherscan's
free rate limit (5 req/s, 100k req/day). ETH blocks are ~12s apart, so even
an hourly window is ~300 blocks -- far more than we can afford to fetch
one-by-one on the free tier. If a gap between runs lets more blocks pile up
than MAX_BLOCKS_PER_RUN, the scan window jumps forward to the most recent
blocks rather than working through the backlog in order, so alerts are
always about what just happened, never something hours old. This is a
best-effort recent-blocks sample, not exhaustive chain coverage.

Each block's own timestamp is also checked against MAX_AGE_MINUTES (not
just its position in the last-N-blocks window), so a stale block doesn't
get reported as if it just happened -- same reasoning as whale_btc.py.
"""
import logging
import os
import time

import requests

logger = logging.getLogger("tickerwatch.whale_eth")

BASE_URL = "https://api.etherscan.io/v2/api"
TIMEOUT = 20
MAX_BLOCKS_PER_RUN = 30
MAX_AGE_MINUTES = 65  # hourly cadence + a little slack for cron/run timing
WEI_PER_ETH = 10**18
CHAIN_ID = 1  # Ethereum mainnet


def _rpc_params(**extra):
    params = {"chainid": CHAIN_ID, "apikey": os.environ["ETHERSCAN_API_KEY"]}
    params.update(extra)
    return params


def _get_latest_block_number():
    resp = requests.get(
        BASE_URL, params=_rpc_params(module="proxy", action="eth_blockNumber"), timeout=TIMEOUT
    )
    resp.raise_for_status()
    return int(resp.json()["result"], 16)


def _get_block(block_number):
    resp = requests.get(
        BASE_URL,
        params=_rpc_params(
            module="proxy",
            action="eth_getBlockByNumber",
            tag=hex(block_number),
            boolean="true",
        ),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def find_large_transactions(last_seen_block, min_usd, eth_usd_price):
    latest = _get_latest_block_number()
    if last_seen_block is None:
        return latest, []
    if not eth_usd_price:
        return last_seen_block, []

    min_eth = min_usd / eth_usd_price
    start = last_seen_block + 1
    if start > latest:
        return last_seen_block, []
    if latest - start + 1 > MAX_BLOCKS_PER_RUN:
        start = latest - MAX_BLOCKS_PER_RUN + 1
    end = latest

    cutoff = time.time() - MAX_AGE_MINUTES * 60
    findings = []
    for block_number in range(start, end + 1):
        try:
            block = _get_block(block_number)
        except Exception:
            logger.exception("Failed to fetch ETH block %s", block_number)
            continue
        if not block:
            continue
        if int(block.get("timestamp", "0x0"), 16) < cutoff:
            continue  # one of the last N blocks by count, but actually stale -- skip its txs
        for tx in block.get("transactions", []):
            value_wei = int(tx.get("value", "0x0"), 16)
            eth_amount = value_wei / WEI_PER_ETH
            if eth_amount >= min_eth:
                findings.append(
                    {
                        "txhash": tx.get("hash"),
                        "eth": eth_amount,
                        "usd": eth_amount * eth_usd_price,
                    }
                )
    return end, findings
