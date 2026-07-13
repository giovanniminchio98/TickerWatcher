"""
Free news source: RSS from a whitelisted feed list (config/keywords.json).
CryptoPanic's free Developer API plan was discontinued (removed April 2026)
and NewsAPI.org's free tier contractually forbids production/non-localhost
use, so RSS from established outlets is the only free, ToS-safe option left
for automated news. All feeds in the default config are public, keyless RSS.

Feed URLs occasionally change or get retired -- a broken feed is logged and
skipped, never a hard failure for the whole run (see main.py's per-source
try/except).
"""
import logging
import re

import feedparser

logger = logging.getLogger("tickerwatch.news_rss")


def _matches_keywords(text, keywords):
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def fetch_matching_articles(rss_feeds, keywords, already_posted_urls, max_articles):
    """Returns up to max_articles dicts: {"title", "summary", "url", "source", "matched_keywords"}"""
    matches = []
    for feed_cfg in rss_feeds:
        if not feed_cfg.get("whitelisted"):
            continue
        try:
            parsed = feedparser.parse(feed_cfg["url"])
        except Exception:
            logger.exception("Failed to parse RSS feed %s", feed_cfg["name"])
            continue
        if parsed.bozo and not parsed.entries:
            logger.warning("RSS feed %s returned no usable entries", feed_cfg["name"])
            continue
        for entry in parsed.entries:
            url = entry.get("link")
            if not url or url in already_posted_urls:
                continue
            title = entry.get("title", "")
            summary = re.sub("<[^<]+?>", "", entry.get("summary", ""))  # strip HTML tags
            hit_keywords = _matches_keywords(f"{title} {summary}", keywords)
            if hit_keywords:
                matches.append(
                    {
                        "title": title,
                        "summary": summary,
                        "url": url,
                        "source": feed_cfg["name"],
                        "matched_keywords": hit_keywords,
                    }
                )
            if len(matches) >= max_articles:
                return matches
    return matches
