"""Post type 1 (highest priority): whale/on-chain alerts. See sources/whale_btc.py
and sources/whale_eth.py for the free-tier data sources and their trade-offs.

The asset symbol uses a $ cashtag ($BTC/$ETH) rather than plain text --
confirmed via a live billing test that this does NOT trigger the $0.20
"post contains a link" surcharge (X's Smart Cashtags are a distinct,
in-app-only entity type, never an external URL). X allows only ONE cashtag
per post (403 Forbidden otherwise), which is why the market-context line
below uses a plain-text symbol instead of a second cashtag.

Siren count scales with size (more sirens = bigger transaction, capped at
10 for $200M+) so the visual weight matches the news.

The first BTC (and first ETH) alert of each run also carries a same-asset
market context line (price + 24h change + a green/red dot) so it doesn't
read as just a bare number -- e.g. a big BTC transfer alongside "BTC is up
5% today" is more informative than either fact alone. Only the first alert
per asset per run gets this line; later alerts for the same asset in the
same run would just repeat near-identical numbers. This costs nothing
extra either way: the price data is already fetched once per run for the
whole pipeline (ctx.prices), so no additional API calls. If that data's
unavailable for some reason, the line is just omitted rather than blocking
the alert.

No coin logo/media on the post itself -- tried and pulled back, looked bad
in practice. The tx hash/explorer link is also gone from the X post (kept
clean/cheap), but the Telegram channel copy still gets the real
block-explorer link (blockchain.com/etherscan) since Telegram is free --
same "X stays lean, Telegram gets the generous version" pattern as news
alerts' source URL.

capped at thresholds.whale.max_alerts_per_run per chain per run (BTC and ETH
each have their own independent counter below), so a busy run can't turn
into a wall of alerts for a single chain. Also capped at
thresholds.whale.max_alerts_per_day combined across both chains, to leave
room in the daily post volume/budget for other post types (e.g. the
comment-engagement replies)."""
import logging
import math

from src.formatting import dot_for_change, fmt_pct, fmt_price, fmt_usd_compact, truncate
from src.sources import whale_btc, whale_eth

logger = logging.getLogger("tickerwatch.triggers.whale")

SIREN_UNIT_USD = 20_000_000  # one siren per $20M, capped at 10 (reached at $200M+)
MAX_SIRENS = 10
BTC_EXPLORER = "https://www.blockchain.com/explorer/transactions/btc/{}"
ETH_EXPLORER = "https://etherscan.io/tx/{}"


def _siren_count(usd):
    if not usd:
        return "🚨"
    count = max(1, min(MAX_SIRENS, math.ceil(usd / SIREN_UNIT_USD)))
    return "🚨" * count


def _asset_context_line(ctx, coingecko_id, symbol):
    # plain symbol, NOT a $cashtag -- the main alert line above already carries
    # the one cashtag X allows per post (Forbidden 403: "Posts are limited to
    # a maximum of one cashtag"), so a second one here would reject the whole post
    info = ctx.prices.get(coingecko_id)
    if not info or info.get("usd") is None:
        return None
    price = info["usd"]
    change = info.get("usd_24h_change")
    return f"{dot_for_change(change)} {symbol}: ${fmt_price(price)} ({fmt_pct(change)} today)"


def _roll_daily_count(state, today_str):
    if state.get("posted_date") != today_str:
        state["posted_date"] = today_str
        state["posted_count_today"] = 0


def _daily_cap_reached(ctx):
    daily_cap = ctx.config["thresholds"]["whale"].get("max_alerts_per_day")
    if daily_cap is None:
        return False
    return ctx.state["whale"].get("posted_count_today", 0) >= daily_cap


def _post(ctx, text, context_line, explorer_url=None):
    parts = [text]
    if context_line:
        parts.append(context_line)
    full_text = truncate("\n\n".join(parts))
    tweet_id = ctx.x.post(full_text)
    if not tweet_id:
        return False
    channel_link = ("View transaction", explorer_url) if explorer_url else None
    ctx.budget.record_spend(has_link=False, text=full_text, channel_link=channel_link)
    ctx.state["whale"]["posted_count_today"] = ctx.state["whale"].get("posted_count_today", 0) + 1
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
    context_shown = False
    seen = set(state["seen_btc_txids"])
    for hit in hits:
        if posted >= th["max_alerts_per_run"] or _daily_cap_reached(ctx):
            break
        if hit["txid"] in seen:
            continue
        if not ctx.budget.can_spend(has_link=False):
            break
        sirens = _siren_count(hit["usd"])
        usd_part = f" ({fmt_usd_compact(hit['usd'])})" if hit["usd"] else ""
        text = f"{sirens} WHALE ALERT\n{hit['btc']:.1f} $BTC{usd_part} just moved on-chain\n#BTC #Crypto"
        context_line = None if context_shown else _asset_context_line(ctx, "bitcoin", "BTC")
        explorer_url = BTC_EXPLORER.format(hit["txid"])
        if _post(ctx, text, context_line, explorer_url):
            state["seen_btc_txids"].append(hit["txid"])
            posted += 1
            fired = True
            context_shown = True
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
    context_shown = False
    for hit in hits:
        if posted >= th["max_alerts_per_run"] or _daily_cap_reached(ctx):
            break
        if not ctx.budget.can_spend(has_link=False):
            break
        sirens = _siren_count(hit["usd"])
        text = f"{sirens} WHALE ALERT\n{hit['eth']:.1f} $ETH ({fmt_usd_compact(hit['usd'])}) just moved on-chain\n#ETH #Crypto"
        context_line = None if context_shown else _asset_context_line(ctx, "ethereum", "ETH")
        explorer_url = ETH_EXPLORER.format(hit["txhash"])
        if _post(ctx, text, context_line, explorer_url):
            posted += 1
            fired = True
            context_shown = True
    return fired


def run(ctx):
    _roll_daily_count(ctx.state["whale"], ctx.now.strftime("%Y-%m-%d"))
    btc_fired = _post_btc_alerts(ctx)
    eth_fired = _post_eth_alerts(ctx)
    return btc_fired or eth_fired
