"""Post type 2: high-relevance "JUST IN" news alerts, RSS-sourced (see
sources/news_rss.py for why CryptoPanic/NewsAPI aren't viable free options).

The main post carries no clickable link -- X's algorithm has suppressed
reach on linked posts hard since March 2026 (near-zero reach for non-Premium
accounts), so the source URL goes in a cheap follow-up reply instead, same
pattern as whale alerts. Unlike whale alerts' tx reference, this link still
costs $0.20 wherever it lives, so this isn't a cost optimization -- it's a
reach one. To make sure a post is never left fully uncited if the reply
happens to fail, the main post still names the outlet (no URL) as a fallback
citation. Still bounded by keywords.max_articles_per_day, the main cost lever
now that whale alerts dropped their link entirely.

The Telegram channel copy always gets the real article URL, regardless of
whether the paid X reply above ends up firing -- Telegram is free, so there's
no budget reason to ever hold that link back there.

Also attaches a small themed red/green/gray trend-line graphic (see
src/media.py's get_trend_media_id) based on the same Claude call's sentiment
read, matching the "chart snippet + terse JUST IN line" style other crypto
news accounts use. Only available on the Claude paraphrase path -- the
mechanical fallback has no sentiment signal, so no image gets attached then."""
import logging

from src.formatting import truncate
from src.media import get_trend_media_id
from src.sources import news_rss, paraphrase

logger = logging.getLogger("tickerwatch.triggers.news")


def run(ctx):
    state = ctx.state["news"]
    kw_cfg = ctx.config["keywords"]

    today_str = ctx.now.strftime("%Y-%m-%d")
    if state.get("posted_date") != today_str:
        state["posted_date"] = today_str
        state["posted_count_today"] = 0

    max_per_day = kw_cfg.get("max_articles_per_day", 3)
    remaining_today = max_per_day - state.get("posted_count_today", 0)
    if remaining_today <= 0:
        return False

    try:
        articles = news_rss.fetch_matching_articles(
            kw_cfg["rss_feeds"],
            kw_cfg["keywords"],
            set(state["posted_urls"]),
            min(kw_cfg.get("max_articles_per_run", 3), remaining_today),
        )
    except Exception:
        logger.exception("News fetch failed")
        return False

    fired = False
    for article in articles:
        if state["posted_count_today"] >= max_per_day:
            break
        if not ctx.budget.can_spend(has_link=False):
            break
        try:
            summary, sentiment = paraphrase.paraphrase_with_sentiment(article["title"], article["summary"])
        except Exception:
            logger.exception("Paraphrase failed for %s", article["url"])
            continue

        text = truncate(f"🚨 JUST IN: {summary}\n(via {article['source']})")
        media_id = get_trend_media_id(ctx, sentiment)
        tweet_id = ctx.x.post(text, media_id=media_id)
        if not tweet_id:
            continue

        channel_text = f"{text}\nSource: {article['url']}"
        ctx.budget.record_spend(has_link=False, text=text, channel_text=channel_text)
        state["posted_urls"].append(article["url"])
        state["posted_count_today"] += 1
        fired = True

        if ctx.budget.can_spend(has_link=True):
            reply_text = truncate(f"Source: {article['url']}")
            reply_id = ctx.x.reply(reply_text, tweet_id)
            if reply_id:
                # already mirrored to the channel above via channel_text, skip duplicate
                ctx.budget.record_spend(has_link=True, text=reply_text, mirror_to_channel=False)

    state["posted_urls"] = state["posted_urls"][-500:]
    return fired
