"""Post type 2: high-relevance "JUST IN" news alerts, RSS-sourced (see
sources/news_rss.py for why CryptoPanic/NewsAPI aren't viable free options).

News is the only post type that still carries a link (required for sourcing),
so it's the main cost driver -- bounded by keywords.max_articles_per_day on
top of the per-run cap and the overall budget cap."""
import logging

from src.formatting import truncate
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
            ctx.budget.record_spend(has_link=True, text=text)
            state["posted_urls"].append(article["url"])
            state["posted_count_today"] += 1
            fired = True

    state["posted_urls"] = state["posted_urls"][-500:]
    return fired
