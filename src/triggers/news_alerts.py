"""Post type 2: high-relevance "JUST IN" news alerts, RSS-sourced (see
sources/news_rss.py for why CryptoPanic/NewsAPI aren't viable free options)."""
import logging

from src.formatting import truncate
from src.sources import news_rss, paraphrase

logger = logging.getLogger("tickerwatch.triggers.news")


def run(ctx):
    state = ctx.state["news"]
    kw_cfg = ctx.config["keywords"]
    try:
        articles = news_rss.fetch_matching_articles(
            kw_cfg["rss_feeds"],
            kw_cfg["keywords"],
            set(state["posted_urls"]),
            kw_cfg.get("max_articles_per_run", 3),
        )
    except Exception:
        logger.exception("News fetch failed")
        return False

    fired = False
    for article in articles:
        if not ctx.budget.can_spend(has_link=True):
            break
        try:
            summary = paraphrase.paraphrase(article["title"], article["summary"])
        except Exception:
            logger.exception("Paraphrase failed for %s", article["url"])
            continue
        text = truncate(f"🚨 JUST IN: {summary}\nSource: {article['url']}")
        tweet_id = ctx.x.post(text)
        if tweet_id:
            ctx.budget.record_spend(has_link=True)
            state["posted_urls"].append(article["url"])
            fired = True

    state["posted_urls"] = state["posted_urls"][-500:]
    return fired
