"""Post type 2: high-relevance "JUST IN" news alerts, RSS-sourced (see
sources/news_rss.py for why CryptoPanic/NewsAPI aren't viable free options).

No link, ever, on X -- same account-wide rule as ai_manager.py: X's algorithm
has suppressed reach on linked posts hard since March 2026 (near-zero reach
for non-Premium accounts), and a link reply still cost real budget besides.
The main post names the outlet (no URL) as its citation instead. Paced by
keywords.json's max_articles_per_run/throttle_after_daily_posts/
throttled_max_articles_per_run (2026-07-22: no hard daily stop anymore --
crossing the daily count throttles the per-run pace down instead of
blocking outright, so a busy news day still gets a steady trickle rather
than going silent; ctx.budget's shared monthly cap is the real backstop
against runaway spend).

Every post also gets a mandatory plain-language explanation posted as a
reply right underneath it (src/sources/paraphrase.py's third output line) --
same "explain the news, not just headline it" pattern as ai_manager's
mandatory second_part, and same reasoning for why it's a reply instead of
crammed into the main post: the main line stays a terse, skimmable headline,
the reply is where the actual "what this means" value is. No link there
either -- just text, same as everywhere else on this account now.

The explanation is mandatory to post AT ALL now (2026-07-22) -- no
explanation, no post, main headline included. Confirmed live that a bare
headline can be genuinely meaningless without it (a "here's why bitcoin
bulls should take a closer look at interest rates" headline that never
got its "why" explained, since the reply carrying that never went out).
This mostly affects the mechanical fallback path (no ANTHROPIC_API_KEY,
or the LLM call failed) which never has an explanation to give -- that
path now produces no post at all rather than a contextless one.

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
import random
import time

from src import story_history
from src.formatting import MAX_TWEET_LEN, truncate
from src.media import get_trend_media_id
from src.sources import news_rss, paraphrase

logger = logging.getLogger("tickerwatch.triggers.news")

# A varied pointer line telling readers the reply underneath is the
# mandatory explanation, on-topic, not some other user's unrelated reply --
# same reasoning as ai_manager's pointer-to-second_part sentence, just
# adapted to this trigger's terse headline+source format instead of a full
# sentence. Randomly chosen (2026-07-22) so it doesn't read as the same
# fixed line every time; only appended if it actually fits without
# truncating away real content (headline or source) to make room -- see
# run()'s use below.
_REPLY_POINTERS = (
    "Explanation below:",
    "Why it matters:",
    "Context below:",
    "More on this below:",
    "The details:",
)

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

    # No hard daily stop (2026-07-22) -- posted_count_today crossing
    # throttle_after_daily_posts no longer blocks posting for the rest of
    # the day, it just throttles how many go out per run from then on
    # (down to throttled_max_articles_per_run, typically 1) so a genuinely
    # busy news day still gets a steady trickle of posts through the
    # evening instead of going silent once an arbitrary count is hit. The
    # real safety net against runaway spend is ctx.budget's shared,
    # absolute monthly cap -- this was always a pacing knob, not a cost
    # control.
    if state.get("posted_count_today", 0) >= kw_cfg.get("throttle_after_daily_posts", 24):
        max_per_run = kw_cfg.get("throttled_max_articles_per_run", 1)
    else:
        max_per_run = kw_cfg.get("max_articles_per_run", 3)

    already_posted = set(state["posted_urls"]) | story_history.recent_urls(ctx.state, ctx.now.timestamp())
    try:
        articles = news_rss.fetch_matching_articles(
            kw_cfg["rss_feeds"],
            kw_cfg["keywords"],
            already_posted,
            max_per_run,
        )
    except Exception:
        logger.exception("News fetch failed")
        return False

    fired = False
    for article in articles:
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

        # explanation is now mandatory to post at all (2026-07-22) -- a
        # headline alone can be meaningless without it (confirmed live: a
        # bitcoin/interest-rates headline that only teased "here's why"
        # with no reply ever explaining why). Covers both the mechanical
        # fallback path (no ANTHROPIC_API_KEY, or the LLM call failed --
        # always returns explanation=None) and the rarer case of an
        # LLM response that validly parsed but only had 2 lines, no third.
        # Previously this only skipped the reply and still posted the
        # bare headline regardless.
        if not explanation or not explanation.strip():
            logger.warning("Skipping article with no explanation to pair with it: %s", article["url"])
            continue

        base_text = f"🚨 JUST IN: {summary}\n(via {article['source']})"
        pointer = random.choice(_REPLY_POINTERS)
        with_pointer = f"{base_text}\n{pointer}"
        # Only add it if it genuinely fits as a bonus -- never truncate
        # away real headline/source content just to make room for it.
        text = truncate(with_pointer if len(with_pointer) <= MAX_TWEET_LEN else base_text)
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
