"""Post type 2: high-relevance "JUST IN" news alerts, RSS-sourced (see
sources/news_rss.py for why CryptoPanic/NewsAPI aren't viable free options).

No link, ever, on X -- same account-wide rule as ai_manager.py: X's algorithm
has suppressed reach on linked posts hard since March 2026 (near-zero reach
for non-Premium accounts), and a link reply still cost real budget besides.
The main post names the outlet (no URL) as its citation instead. Still
bounded by keywords.max_articles_per_day, the main cost lever now that whale
alerts dropped their link entirely too.

Every post also gets a mandatory plain-language explanation posted as a
reply right underneath it (src/sources/paraphrase.py's third output line) --
same "explain the news, not just headline it" pattern as ai_manager's
mandatory second_part, and same reasoning for why it's a reply instead of
crammed into the main post: the main line stays a terse, skimmable headline,
the reply is where the actual "what this means" value is. No link there
either -- just text, same as everywhere else on this account now. Only
available on the Claude paraphrase path; the mechanical fallback (no
ANTHROPIC_API_KEY) has no explanation to give, so that post goes out
without a reply rather than with an empty or fabricated one.

The Telegram channel copy always gets the real article URL -- Telegram is
free, so there's no reason to ever hold that link back there, same
"Telegram can be more generous than X" pattern used everywhere else.

Dedup checks src/story_history.py (a shared, cross-trigger, time-windowed
memory) in addition to this trigger's own posted_urls -- confirmed live
that without this, the same real-world story got covered here and by
ai_manager.py within hours of each other, since neither trigger knew what
the other had already posted.

Also attaches a small themed red/green/gray trend-line graphic (see
src/media.py's get_trend_media_id) based on the same Claude call's sentiment
read, matching the "chart snippet + terse JUST IN line" style other crypto
news accounts use. Only available on the Claude paraphrase path -- the
mechanical fallback has no sentiment signal, so no image gets attached then."""
import logging
import time

from src import story_history
from src.formatting import truncate
from src.media import get_trend_media_id
from src.sources import news_rss, paraphrase

logger = logging.getLogger("tickerwatch.triggers.news")

# Gap between successive articles' main posts within the same run (not
# between a post and its own reply, which stays immediate -- that pairing
# already reads as normal, expected structure). Several unrelated headlines
# landing seconds apart reads as an obvious bot dump; spacing them out is
# cheap insurance for looking more like a person actually posting, and
# possibly for reach too (X's own ranking is dwell-time/engagement based,
# and multiple posts within the same minute don't get time to be seen
# individually before the next one buries it). main.py's per-trigger
# watchdog timeout is overridden for this trigger (see _TRIGGER_TIMEOUTS)
# to leave room for these gaps -- the default 120s wouldn't survive even
# one.
_INTER_ARTICLE_DELAY_SECONDS = 120


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

    already_posted = set(state["posted_urls"]) | story_history.recent_urls(ctx.state, ctx.now.timestamp())
    try:
        articles = news_rss.fetch_matching_articles(
            kw_cfg["rss_feeds"],
            kw_cfg["keywords"],
            already_posted,
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
        if fired:
            time.sleep(_INTER_ARTICLE_DELAY_SECONDS)
        try:
            summary, sentiment, explanation = paraphrase.paraphrase_with_sentiment(
                article["title"], article["summary"]
            )
        except Exception:
            logger.exception("Paraphrase failed for %s", article["url"])
            continue

        # Belt-and-suspenders alongside paraphrase.py's own format check --
        # covers the mechanical fallback path too, which just condenses
        # article["title"] and would post a bare "🚨 JUST IN: (via X)" if
        # that title was itself empty/whitespace (a malformed RSS entry).
        if not summary or not summary.strip():
            logger.warning("Skipping article with empty paraphrase result: %s", article["url"])
            continue

        text = truncate(f"🚨 JUST IN: {summary}\n(via {article['source']})")
        media_id = get_trend_media_id(ctx, sentiment)
        tweet_id = ctx.x.post(text, media_id=media_id)
        if not tweet_id:
            continue

        channel_text = f"{text}\n\n{explanation}" if explanation else text
        ctx.budget.record_spend(
            has_link=False, text=text, channel_text=channel_text, channel_link=("Source", article["url"])
        )
        if explanation and ctx.budget.can_spend(has_link=False):
            reply_text = truncate(explanation)
            reply_id = ctx.x.reply(reply_text, tweet_id)
            if reply_id:
                # already mirrored to the channel above via channel_text
                ctx.budget.record_spend(has_link=False, text=explanation, mirror_to_channel=False)
        state["posted_urls"].append(article["url"])
        story_history.add_entry(ctx.state, text=text, url=article["url"], now_ts=ctx.now.timestamp())
        state["posted_count_today"] += 1
        fired = True

    state["posted_urls"] = state["posted_urls"][-500:]
    return fired
