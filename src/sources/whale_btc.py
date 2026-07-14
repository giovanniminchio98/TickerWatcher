"""
Free, keyless BTC whale detection via the blockchain.info block explorer API
(no Whale Alert API key needed -- Whale Alert's programmatic API is paid-only
as of 2026; their free tier is web/app viewing only).

Trade-off vs. a paid feed: blockchain.info doesn't tag exchange/wallet
identities, so we never fabricate a "from X wallet to Y wallet" claim -- we
report the real on-chain amount plus a link to the transaction so every post
is independently verifiable, and skip the wallet-type framing entirely.

To bound run time/requests, only the most recent MAX_BLOCKS_PER_RUN blocks
are scanned each run. At a ~10 min average BTC block time this comfortably
covers an hourly check window (usually only ~5-6 new blocks). If a gap
between runs let more blocks pile up than that (a scheduler outage, a long
run, etc.), the scan window jumps forward to the most recent
MAX_BLOCKS_PER_RUN blocks rather than working through the backlog in
order -- an alert should be about what just happened, not something that
happened hours ago just because it's next in a queue. The skipped-over
backlog is never scanned, so this is a best-effort feed, not exhaustive.

Block *count* alone doesn't guarantee freshness though -- BTC block times
are naturally variable (Poisson-distributed around the ~10 min average, so
a run of slow blocks can span well over an hour for the same 6 blocks).
Each block's own timestamp is checked against MAX_AGE_MINUTES too, so a
block that's technically "one of the last 6" but is actually stale gets
skipped from findings (transactions in it are simply never reported, not
deferred to next run) rather than posted as if it just happened.
"""
import logging
import time

import requests

logger = logging.getLogger("tickerwatch.whale_btc")

BASE_URL = "https://blockchain.info"
TIMEOUT = 20
MAX_BLOCKS_PER_RUN = 6
MAX_AGE_MINUTES = 65  # hourly cadence + a little slack for cron/run timing
SATOSHI = 100_000_000


def _get_latest_height():
    resp = requests.get(f"{BASE_URL}/q/getblockcount", timeout=TIMEOUT)
    resp.raise_for_status()
    return int(resp.text)


def _get_block(height):
    resp = requests.get(f"{BASE_URL}/block-height/{height}", params={"format": "json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    blocks = resp.json().get("blocks", [])
    return blocks[0] if blocks else None


def find_large_transactions(last_seen_height, min_btc, btc_usd_price):
    """Returns (new_last_height, [{"txid", "btc", "usd"}]) for transactions
    at or above min_btc, scanning at most MAX_BLOCKS_PER_RUN new blocks."""
    latest = _get_latest_height()
    if last_seen_height is None:
        # first run ever: don't backfill the whole chain, just start tracking from here
        return latest, []

    start = last_seen_height + 1
    if start > latest:
        return last_seen_height, []
    if latest - start + 1 > MAX_BLOCKS_PER_RUN:
        start = latest - MAX_BLOCKS_PER_RUN + 1
    end = latest

    cutoff = time.time() - MAX_AGE_MINUTES * 60
    findings = []
    for height in range(start, end + 1):
        try:
            block = _get_block(height)
        except Exception:
            logger.exception("Failed to fetch BTC block %s", height)
            continue
        if not block:
            continue
        if block.get("time", 0) < cutoff:
            continue  # one of the last N blocks by count, but actually stale -- skip its txs
        for tx in block.get("tx", []):
            outputs = tx.get("out", [])
            if not outputs:
                continue
            # use the single largest output rather than summing all outputs,
            # since summing would double-count change returned to the sender
            largest_out_sat = max(o.get("value", 0) for o in outputs)
            btc_amount = largest_out_sat / SATOSHI
            if btc_amount >= min_btc:
                findings.append(
                    {
                        "txid": tx.get("hash"),
                        "btc": btc_amount,
                        "usd": btc_amount * btc_usd_price if btc_usd_price else None,
                    }
                )
    return end, findings
