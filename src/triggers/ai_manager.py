"""Post type 11 (opt-in via ANTHROPIC_API_KEY presence): fully autonomous
post + reply + repost decision-maker, meant to run unattended for months.

Unlike every other trigger in this pipeline (which is mechanical/templated,
deciding WHETHER to fire but not WHAT to say beyond a fixed format), this one
hands all three decisions to Claude in a single call: whether to publish an
original post right now, which (if any) candidate posts from other monitored
accounts are worth replying to, and which (if any) of those same candidates
are worth reposting (plain retweet or a quote-tweet with Claude's own
comment). See src/sources/ai_manager_brain.py for the prompt/parsing and
README's "AI Manager" section for the full design rationale.

Reposting absorbs retweets.py's old role too (now disabled in main.py) --
that trigger retweeted every new post from every monitored account
unconditionally with zero judgment, which didn't fit "AI decides everything";
here a repost only happens when Claude actually picks it.

The original post decision also absorbs filler.py's old role (now disabled)
of "always post something so the account doesn't go quiet" -- instead of a
mechanical pick from a fixed list every time nothing else fired, Claude sees
a few of those same generic-engagement examples purely as style reference
and may write something in that spirit ONLY if it's genuinely good; posting
nothing is explicitly the preferred outcome over posting mediocre filler.

Runs on its own cadence independent of the hourly workflow schedule --
min_hours_between_calls + max_calls_per_day (config/ai_manager.json) bound it
to roughly 5-10 Claude calls/day even though main.py itself runs hourly.

Absorbs the reply-decision role that config/reply_targets.json + comment_
engagement.py used to own (see main.py -- comment_engagement is disabled by
default now that this trigger covers the same accounts with more judgment);
the config file and its per-account times_per_day cap are reused as-is.

Two independent hard budget caps gate this trigger, each stopping it cleanly
rather than erroring when exhausted:
  - ctx.claude_budget (config/claude_budget.json) -- gates whether the Claude
    call itself is even attempted.
  - ctx.budget (config/budget.json) -- gates whether a decided post/reply is
    actually sent to X (same shared pool every other trigger uses).

Every call sends one Telegram bot-chat audit message (decision + reasoning
for both the post and every reply, or "no action") -- with no manual approval
step, this is the only way to spot-check what it's actually doing over time.
"""
import logging
import random

from src import telegram_client
from src.formatting import fmt_pct, fmt_price, truncate
from src.sources import ai_manager_brain, news_rss, twelvedata


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

logger = logging.getLogger("tickerwatch.triggers.ai_manager")


def _roll_day(state, today_str):
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0
        state["posts_today"] = 0
        state["replies_today"] = 0
        state["reposts_today"] = 0
        for acct in state.get("account_replies_today", {}):
            state["account_replies_today"][acct] = 0
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


def _reply_candidates(ctx, cfg, state):
    """Reuses config/reply_targets.json (same file/auto-resolve pattern as
    comment_engagement.py) as the shared pool of accounts to consider both
    replying to and reposting -- a tweet already acted on (either way) is
    excluded so it never comes up as a candidate again. Candidates from a
    target marked reply_only (smaller accounts added specifically because
    the bigger ones' tweet-level reply restrictions 403 our replies/quotes)
    are tagged so the repost path below never considers them -- reply-only
    is enforced both in the prompt and as a hard code-level filter."""
    candidates = []
    acted_ids = set(state.get("replied_tweet_ids", [])) | set(state.get("reposted_tweet_ids", []))
    reply_caps = state.setdefault("account_replies_today", {})
    repost_caps = state.setdefault("account_reposts_today", {})

    for target in ctx.config["reply_targets"]["targets"]:
        if not target.get("enabled"):
            continue
        handle = target["handle"]
        cap = max(0, target.get("times_per_day", 1))
        if reply_caps.get(handle, 0) >= cap and repost_caps.get(handle, 0) >= cap:
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
        reply_only = target.get("reply_only", False)
        for tweet in tweets:
            if tweet["id"] in acted_ids:
                continue
            candidates.append({
                "handle": handle, "tweet_id": tweet["id"], "text": tweet["text"], "reply_only": reply_only,
            })
    return candidates


def _send_audit_message(decision, post_result, reply_results, repost_results):
    lines = ["🤖 AI Manager decision:"]
    post = decision.get("post") or {}
    if post.get("should_post"):
        status = "posted" if post_result else "attempted (not sent -- budget/cap reached)"
        lines.append(f"\n📝 Post ({status}): {post.get('text')}\nReasoning: {post.get('reasoning', '')}")
    else:
        lines.append(f"\n📝 No post this call. Reasoning: {post.get('reasoning', '(none given)')}")

    replies = decision.get("replies") or []
    if replies:
        for r in reply_results:
            lines.append(
                f"\n💬 Reply to @{r['handle']} ({r['status']}): {r['text']}\nReasoning: {r['reasoning']}"
            )
    else:
        lines.append("\n💬 No replies this call.")

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
        "reply_candidates": _reply_candidates(ctx, cfg, state),
        "own_recent_posts": state.get("recent_post_texts", []),
        "filler_examples": _filler_examples(ctx),
        "max_replies_per_call": cfg["max_replies_per_call"],
        "max_reposts_per_call": cfg["max_reposts_per_call"],
        "prefer_plain_retweets": cfg.get("prefer_plain_retweets", False),
    }

    decision, usage = ai_manager_brain.decide(snapshot, cfg["model"])
    state["last_call_time"] = ctx.now.timestamp()
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        return False

    fired = False
    post_result = None
    post = decision.get("post") or {}
    if post.get("should_post") and post.get("text") and state["posts_today"] < cfg["max_posts_per_day"]:
        if ctx.budget.can_spend(has_link=False):
            text = truncate(post["text"], ai_manager_brain.MAX_POST_LEN)
            tweet_id = ctx.x.post(text)
            if tweet_id:
                ctx.budget.record_spend(has_link=False, text=text)
                state["recent_post_texts"] = (state.get("recent_post_texts", []) + [text])[-10:]
                state["posts_today"] += 1
                post_result = tweet_id
                fired = True

    reply_results = []
    replied_ids = state.setdefault("replied_tweet_ids", [])
    acct_caps = state.setdefault("account_replies_today", {})
    for r in (decision.get("replies") or [])[: cfg["max_replies_per_call"]]:
        idx = r.get("candidate_index")
        if idx is None or idx < 0 or idx >= len(snapshot["reply_candidates"]) or not r.get("text"):
            continue
        candidate = snapshot["reply_candidates"][idx]
        handle = candidate["handle"]
        cap = next(
            (t.get("times_per_day", 1) for t in ctx.config["reply_targets"]["targets"] if t["handle"] == handle),
            1,
        )
        if state["replies_today"] >= cfg["max_replies_per_day"] or acct_caps.get(handle, 0) >= cap:
            continue
        if not ctx.budget.can_spend(has_link=False):
            break

        text = truncate(r["text"], ai_manager_brain.MAX_REPLY_LEN)
        reply_id = ctx.x.reply(text, candidate["tweet_id"])
        status = "sent" if reply_id else "failed"
        reply_results.append({"handle": handle, "text": text, "reasoning": r.get("reasoning", ""), "status": status})
        if reply_id:
            # replies aren't original posts -- keep the public channel a pure
            # mirror of what the account itself writes, not a log of every
            # engagement action (see budget.py's mirror_to_channel)
            ctx.budget.record_spend(has_link=False, text=f"Reply to @{handle}: {text}", mirror_to_channel=False)
            replied_ids.append(candidate["tweet_id"])
            acct_caps[handle] = acct_caps.get(handle, 0) + 1
            state["replies_today"] += 1
            fired = True

    state["replied_tweet_ids"] = replied_ids[-500:]

    repost_results = []
    reposted_ids = state.setdefault("reposted_tweet_ids", [])
    repost_caps = state.setdefault("account_reposts_today", {})
    used_indices = {r.get("candidate_index") for r in (decision.get("replies") or [])}
    for rp in (decision.get("reposts") or [])[: cfg["max_reposts_per_call"]]:
        idx = rp.get("candidate_index")
        if idx is None or idx < 0 or idx >= len(snapshot["reply_candidates"]) or idx in used_indices:
            continue
        action = rp.get("action")
        if action not in ("retweet", "quote"):
            continue
        candidate = snapshot["reply_candidates"][idx]
        if candidate.get("reply_only"):
            # hard safety net -- the prompt already tells Claude not to
            # repost these, but never trust that alone
            continue
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
            # same reasoning as replies above -- a retweet/quote-tweet isn't
            # original content, so it never mirrors to the public channel
            spend_desc = f"{action} of @{handle}'s post {candidate['tweet_id']}"
            ctx.budget.record_spend(has_link=False, text=spend_desc, mirror_to_channel=False)
            reposted_ids.append(candidate["tweet_id"])
            repost_caps[handle] = repost_caps.get(handle, 0) + 1
            state["reposts_today"] += 1
            fired = True

    state["reposted_tweet_ids"] = reposted_ids[-500:]
    _send_audit_message(decision, post_result, reply_results, repost_results)
    return fired
