"""
Free news source: RSS from a whitelisted feed list. CryptoPanic's free
Developer API plan was discontinued (removed April 2026) and NewsAPI.org's
free tier contractually forbids production/non-localhost use, so RSS from
established outlets is the only free, ToS-safe option left for automated
news. All feeds in the default configs are public, keyless RSS.

Two fetch modes:
  - fetch_matching_articles: keyword-gated, used against config/keywords.json's
    finance/crypto/AI feeds by news_alerts.py/content_drafts.py/ai_manager.py's
    (now secondary) crypto-news snapshot.
  - fetch_latest_articles: unconditional, used against config/world_news.json's
    general world-news outlets by ai_manager.py's recap -- "what's the latest"
    doesn't fit a keyword whitelist the way a finance alert does.

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


def fetch_latest_articles(rss_feeds, already_posted_urls, max_per_feed=3):
    """Same fetch/parse/error-isolation shape as fetch_matching_articles, but
    with no keyword gate -- just the max_per_feed most recent entries from
    each whitelisted feed. Built for ai_manager's world-news recap: "what
    are the latest headlines" doesn't fit a finance/crypto keyword whitelist
    the way JUST IN alerts do (a war, an election, a disaster wouldn't match
    any term in config/keywords.json), so this pulls unconditionally and
    leaves judging what's actually important to the synthesis step instead
    of a keyword filter. Returns dicts: {"title", "summary", "url", "source",
    "lang"} -- lang is feed_cfg's own "lang" field (e.g. "it", "fr"),
    defaulting to "en", so the prompt knows which items need translation."""
    articles = []
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
        taken = 0
        for entry in parsed.entries:
            if taken >= max_per_feed:
                break
            url = entry.get("link")
            if not url or url in already_posted_urls:
                continue
            title = entry.get("title", "")
            summary = re.sub("<[^<]+?>", "", entry.get("summary", ""))  # strip HTML tags
            articles.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "source": feed_cfg["name"],
                    "lang": feed_cfg.get("lang", "en"),
                }
            )
            taken += 1
    return articles
