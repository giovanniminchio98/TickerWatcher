"""Post type 11 (opt-in via ANTHROPIC_API_KEY presence): autonomous post +
repost decision-maker, meant to run unattended for months on a slow, fixed
cadence (4-6 posts/day) -- deliberately less frequent than main.py's hourly
schedule, since the whole point of this redesign is a small number of
substantial, recognizable posts rather than a high-volume feed. See
src/sources/ai_manager_brain.py for the prompt/parsing and README's "AI
Manager" section for the full design rationale.

Replies used to live in this same call but now run on their own, much
faster cadence in reply_manager.py -- see that module's docstring for why.
Reposting stays here: it only ever targets non-reply_only (bigger) accounts
(reply_only accounts are reply_manager.py's territory exclusively), and
absorbs retweets.py's old unconditional-retweet role, but only fires when
Claude actually picks a candidate.

Every post always carries an image (via src.sources.image_gen, DALL-E,
opt-in via OPENAI_API_KEY) or, if no image ends up available, a real link
attached as a follow-up reply -- same "link lives in a reply, not the main
post" reach-optimization pattern news_alerts.py already uses. The link is
the real source URL of whichever news article the post is actually based
on (Claude's news_index), when there is one -- otherwise
config/ai_manager.json's fallback_link_url (e.g. the public Telegram
channel). If neither an image nor any link is available, the post still
goes out without either rather than being blocked entirely, since
"post nothing at all" is a worse failure than "post without the extra".

Two independent hard budget caps gate this trigger, each stopping it cleanly
rather than erroring when exhausted:
  - ctx.claude_budget (config/claude_budget.json) -- gates whether the Claude
    call itself is even attempted.
  - ctx.budget (config/budget.json) -- gates whether a decided post/repost is
    actually sent to X (same shared pool every other trigger uses).
A third, independent budget (ctx.image_budget, config/image_budget.json)
gates whether image generation is attempted at all -- exhausting it just
means posts fall back to the link, never a hard stop.

Every call sends one Telegram bot-chat audit message (decision + reasoning
for both the post and every repost, or "no action") -- with no manual
approval step, this is the only way to spot-check what it's actually doing
over time.
"""
import logging
import random

from src import telegram_client
from src.formatting import fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain, image_gen, news_rss, twelvedata

logger = logging.getLogger("tickerwatch.triggers.ai_manager")


def _filler_examples(ctx, n=5):
    """A small random sample from config/filler.json's generic engagement
    prompts, handed to Claude as style reference only (never posted
    verbatim) for the rare case nothing data-driven is post-worthy but a
    genuine, non-rubbish generic post would still be nice -- see
    ai_manager_brain._build_prompt's "GENERIC ENGAGEMENT EXAMPLES" section.
    Absorbs filler.py's old role of "always post something," but only as an
    option Claude can take or leave, not a mechanical last resort."""
    posts = ctx.config.get("filler", {}).get("posts", [])
    if not posts:
        return []
    return random.sample(posts, min(n, len(posts)))


def _roll_day(state, today_str):
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0
        state["posts_today"] = 0
        state["reposts_today"] = 0
        for acct in state.get("account_reposts_today", {}):
            state["account_reposts_today"][acct] = 0


def _ready_for_call(ctx, cfg, state):
    if state["calls_today"] >= cfg["max_calls_per_day"]:
        return False
    last_call = state.get("last_call_time")
    if last_call is None:
        return True
    hours_since = (ctx.now.timestamp() - last_call) / 3600
    # small random jitter so the cadence isn't perfectly clockwork
    required_gap = cfg["min_hours_between_calls"] + random.uniform(0, 0.5)
    return hours_since >= required_gap


def _price_snapshot_lines(ctx):
    lines = []
    for asset in ctx.config["watchlist"]["crypto"]:
        info = ctx.prices.get(asset["coingecko_id"])
        if not info or info.get("usd") is None:
            continue
        lines.append(
            f"{asset['symbol']}: ${fmt_price(info['usd'])} ({fmt_pct(info.get('usd_24h_change'))} 24h)"
        )
    for asset in ctx.config["watchlist"].get("stocks", []):
        try:
            q = twelvedata.get_quote(asset["symbol"])
        except Exception:
            logger.exception("Twelve Data quote failed for %s", asset["symbol"])
            continue
        if q:
            lines.append(f"{asset['symbol']}: ${fmt_price(q['price'])} ({fmt_pct(q['percent_change'])})")
    return lines


def _news_snapshot(ctx, limit=6):
    kw_cfg = ctx.config["keywords"]
    try:
        return news_rss.fetch_matching_articles(kw_cfg["rss_feeds"], kw_cfg["keywords"], set(), limit)
    except Exception:
        logger.exception("News fetch failed for ai_manager")
        return []


def _repost_candidates(ctx, cfg, state):
    """Reuses config/reply_targets.json but excludes reply_only accounts up
    front -- those are reply_manager.py's territory exclusively, and were
    already hard-blocked from reposting anyway. A tweet already reposted is
    excluded so it never comes up as a candidate again."""
    candidates = []
    acted_ids = set(state.get("reposted_tweet_ids", []))
    repost_caps = state.setdefault("account_reposts_today", {})

    for target in ctx.config["reply_targets"]["targets"]:
        if not target.get("enabled") or target.get("reply_only"):
            continue
        handle = target["handle"]
        cap = max(0, target.get("times_per_day", 1))
        if repost_caps.get(handle, 0) >= cap:
            continue

        acct_state = state.setdefault("resolved_accounts", {}).setdefault(handle, {})
        user_id = target.get("user_id") or acct_state.get("resolved_user_id")
        if not user_id:
            user_id = ctx.x.get_user_id(handle)
            if not user_id:
                continue
            acct_state["resolved_user_id"] = user_id

        tweets = ctx.x.get_recent_tweets_with_text(
            user_id, max_results=cfg["max_reply_candidates_per_account"]
        )
        for tweet in tweets:
            if tweet["id"] in acted_ids:
                continue
            candidates.append({"handle": handle, "tweet_id": tweet["id"], "text": tweet["text"]})
    return candidates


def _preferred_link(ctx, snapshot, post):
    """The real source URL of the news article this post is actually based
    on (post['news_index']), if there is one -- otherwise config/ai_manager
    .json's generic fallback_link_url (e.g. the Telegram channel)."""
    idx = post.get("news_index")
    if idx is not None and isinstance(idx, int) and 0 <= idx < len(snapshot["news"]):
        return snapshot["news"][idx]["url"]
    return ctx.config["ai_manager"].get("fallback_link_url")


def _attach_image_or_link(ctx, image_prompt, fallback_url):
    """Returns (media_id_or_None, link_url_or_None) -- exactly one of these
    is populated when an image or a link fallback is actually available,
    both are None if neither could be produced (a post still goes out
    text-only rather than being blocked entirely)."""
    if image_prompt and ctx.image_budget.can_spend():
        image_bytes = image_gen.generate_post_image(image_prompt)
        if image_bytes:
            media_id = ctx.x.upload_media(image_bytes)
            if media_id:
                ctx.image_budget.record_spend()
                return media_id, None

    if fallback_url:
        return None, fallback_url
    return None, None


def _send_audit_message(decision, post_status, repost_results):
    lines = ["🤖 AI Manager decision:"]
    post = decision.get("post") or {}
    if post.get("should_post"):
        lines.append(f"\n📝 Post ({post_status}): {post.get('text')}\nReasoning: {post.get('reasoning', '')}")
    else:
        lines.append(f"\n📝 No post this call. Reasoning: {post.get('reasoning', '(none given)')}")

    reposts = decision.get("reposts") or []
    if reposts:
        for rp in repost_results:
            label = "Quote-tweet" if rp["action"] == "quote" else "Retweet"
            extra = f": {rp['text']}" if rp.get("text") else ""
            lines.append(
                f"\n🔁 {label} of @{rp['handle']} ({rp['status']}){extra}\nReasoning: {rp['reasoning']}"
            )
    else:
        lines.append("\n🔁 No reposts this call.")

    telegram_client.send_message("\n".join(lines))


def run(ctx):
    cfg = ctx.config["ai_manager"]
    state = ctx.state["ai_manager"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    _roll_day(state, today_str)

    if not _ready_for_call(ctx, cfg, state):
        return False
    if not ctx.claude_budget.can_spend():
        logger.info("ai_manager: Claude budget exhausted this month, skipping call")
        return False

    snapshot = {
        "prices": _price_snapshot_lines(ctx),
        "news": _news_snapshot(ctx),
        "repost_candidates": _repost_candidates(ctx, cfg, state),
        "own_recent_posts": state.get("recent_post_texts", []),
        "filler_examples": _filler_examples(ctx),
        "max_reposts_per_call": cfg["max_reposts_per_call"],
        "prefer_plain_retweets": cfg.get("prefer_plain_retweets", False),
    }

    decision, usage = ai_manager_brain.decide(snapshot, cfg["model"])
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        # outright API failure or an unparseable response -- don't start the
        # cadence cooldown on a call that produced nothing usable, so the
        # very next hourly run retries immediately instead of waiting out a
        # full cooldown for a call that never actually happened. calls_today
        # still increments either way, so a persistently broken call can't
        # retry more than max_calls_per_day times in one day.
        return False

    # only a successfully parsed decision starts the real cooldown
    state["last_call_time"] = ctx.now.timestamp()

    fired = False
    post_status = "skipped -- daily post cap reached"
    post = decision.get("post") or {}
    if post.get("should_post") and post.get("text") and state["posts_today"] < cfg["max_posts_per_day"]:
        if not ctx.budget.can_spend(has_link=False):
            post_status = "not sent -- X budget cap reached"
        else:
            text = truncate(post["text"], ai_manager_brain.MAX_POST_LEN)
            fallback_url = _preferred_link(ctx, snapshot, post)
            media_id, link_url = _attach_image_or_link(ctx, post.get("image_prompt"), fallback_url)
            tweet_id = ctx.x.post(text, media_id=media_id)
            if tweet_id:
                channel_link = ("Read more", link_url) if link_url else None
                ctx.budget.record_spend(has_link=False, text=text, channel_link=channel_link)
                if link_url and ctx.budget.can_spend(has_link=True):
                    reply_id = ctx.x.reply(truncate(link_url), tweet_id)
                    if reply_id:
                        # already mirrored to the channel above via channel_link, skip duplicate
                        ctx.budget.record_spend(has_link=True, text=link_url, mirror_to_channel=False)
                state["recent_post_texts"] = (state.get("recent_post_texts", []) + [text])[-10:]
                state["posts_today"] += 1
                post_status = "posted"
                fired = True
            else:
                # ctx.x.post() itself failed (X API error) -- distinct from a
                # budget/cap block, and ops_alerts already fired for this
                post_status = "failed -- X API call error, see ops_alerts"

    repost_results = []
    reposted_ids = state.setdefault("reposted_tweet_ids", [])
    repost_caps = state.setdefault("account_reposts_today", {})
    for rp in (decision.get("reposts") or [])[: cfg["max_reposts_per_call"]]:
        idx = rp.get("candidate_index")
        if idx is None or idx < 0 or idx >= len(snapshot["repost_candidates"]):
            continue
        action = rp.get("action")
        if action not in ("retweet", "quote"):
            continue
        candidate = snapshot["repost_candidates"][idx]
        handle = candidate["handle"]
        cap = next(
            (t.get("times_per_day", 1) for t in ctx.config["reply_targets"]["targets"] if t["handle"] == handle),
            1,
        )
        if state["reposts_today"] >= cfg["max_reposts_per_day"] or repost_caps.get(handle, 0) >= cap:
            continue
        if not ctx.budget.can_spend(has_link=False):
            break

        if action == "quote":
            text = truncate(rp.get("text") or "", ai_manager_brain.MAX_QUOTE_LEN)
            result_id = ctx.x.post(text, quote_tweet_id=candidate["tweet_id"])
        else:
            text = None
            result_id = ctx.x.retweet(candidate["tweet_id"]) and candidate["tweet_id"]

        status = "sent" if result_id else "failed"
        repost_results.append({
            "handle": handle, "action": action, "text": text,
            "reasoning": rp.get("reasoning", ""), "status": status,
        })
        if result_id:
            # a retweet/quote-tweet isn't original content -- never mirror to the public channel
            spend_desc = f"{action} of @{handle}'s post {candidate['tweet_id']}"
            ctx.budget.record_spend(has_link=False, text=spend_desc, mirror_to_channel=False)
            reposted_ids.append(candidate["tweet_id"])
            repost_caps[handle] = repost_caps.get(handle, 0) + 1
            state["reposts_today"] += 1
            fired = True

    state["reposted_tweet_ids"] = reposted_ids[-500:]
    _send_audit_message(decision, post_status, repost_results)
    return fired
