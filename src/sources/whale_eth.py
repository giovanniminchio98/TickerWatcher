"""
Free ETH whale detection via Etherscan's free API key (the "proxy" module,
which just wraps standard Ethereum JSON-RPC and is available on the free
tier -- no Whale Alert subscription needed).

Same bounded-scan trade-off as whale_btc.py: only the most recent
MAX_BLOCKS_PER_RUN blocks are scanned per run to stay well within Etherscan's
free rate limit (5 req/s, 100k req/day). ETH blocks are ~12s apart, so a 3-4h
window is ~900-1200 blocks -- far more than we can afford to fetch one-by-one
on the free tier, so this is a best-effort recent-blocks sample, not
exhaustive chain coverage. Document this trade-off for the user in the README.
"""
import logging
import os

import requests

logger = logging.getLogger("tickerwatch.whale_eth")

BASE_URL = "https://api.etherscan.io/v2/api"
TIMEOUT = 20
MAX_BLOCKS_PER_RUN = 30
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
    end = min(latest, start + MAX_BLOCKS_PER_RUN - 1)
    if start > latest:
        return last_seen_block, []

    findings = []
    for block_number in range(start, end + 1):
        try:
            block = _get_block(block_number)
        except Exception:
            logger.exception("Failed to fetch ETH block %s", block_number)
            continue
        if not block:
            continue
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
