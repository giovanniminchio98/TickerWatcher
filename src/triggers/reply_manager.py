"""Fast-cadence reply decision-maker (opt-in via ANTHROPIC_API_KEY presence),
split out from ai_manager.py specifically to run far more often (roughly
hourly, capped at max_calls_per_day) than the slow 4-6/day post cadence.

Only ever considers accounts marked reply_only in config/reply_targets.json
-- bigger accounts commonly restrict who can reply to their posts (X's
tweet-level conversation-control setting), which made replies to them 403
regardless of content quality when this logic lived inside ai_manager.py.
Those bigger accounts are still reposted (retweet/quote) by ai_manager.py,
just never replied to by this trigger.

Never calls Claude if there are no fresh candidates to consider -- a cheap
mechanical check runs first, so a quiet hour costs nothing rather than
spending a Claude call to be told "no replies."

Same two-budget gating as ai_manager.py: ctx.claude_budget gates whether the
call is attempted, ctx.budget gates whether a decided reply is actually
sent to X.
"""
import logging
import random

from src import telegram_client
from src.formatting import truncate
from src.sources import reply_manager_brain

logger = logging.getLogger("tickerwatch.triggers.reply_manager")


def _roll_day(state, today_str):
    if state.get("date") != today_str:
        state["date"] = today_str
        state["calls_today"] = 0
        state["replies_today"] = 0
        for acct in state.get("account_replies_today", {}):
            state["account_replies_today"][acct] = 0


def _ready_for_call(ctx, cfg, state):
    if state["calls_today"] >= cfg["max_calls_per_day"]:
        return False
    if state["replies_today"] >= cfg["max_replies_per_day"]:
        return False
    last_call = state.get("last_call_time")
    if last_call is None:
        return True
    hours_since = (ctx.now.timestamp() - last_call) / 3600
    required_gap = cfg["min_hours_between_calls"] + random.uniform(0, 0.2)
    return hours_since >= required_gap


def _candidates(ctx, cfg, state):
    candidates = []
    acted_ids = set(state.get("replied_tweet_ids", []))
    reply_caps = state.setdefault("account_replies_today", {})

    for target in ctx.config["reply_targets"]["targets"]:
        if not target.get("enabled") or not target.get("reply_only"):
            continue
        handle = target["handle"]
        cap = max(0, target.get("times_per_day", 1))
        if reply_caps.get(handle, 0) >= cap:
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


def _send_audit_message(reply_results):
    if not reply_results:
        return
    lines = ["💬 Reply Manager decision:"]
    for r in reply_results:
        lines.append(f"\nReply to @{r['handle']} ({r['status']}): {r['text']}\nReasoning: {r['reasoning']}")
    telegram_client.send_message("\n".join(lines))


def run(ctx):
    cfg = ctx.config["reply_manager"]
    state = ctx.state["reply_manager"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    _roll_day(state, today_str)

    if not _ready_for_call(ctx, cfg, state):
        return False

    candidates = _candidates(ctx, cfg, state)
    if not candidates:
        return False
    if not ctx.claude_budget.can_spend():
        logger.info("reply_manager: Claude budget exhausted this month, skipping call")
        return False

    snapshot = {
        "candidates": candidates,
        "own_recent_posts": ctx.state.get("ai_manager", {}).get("recent_post_texts", []),
        "max_replies_per_call": cfg["max_replies_per_call"],
    }

    decision, usage = reply_manager_brain.decide(snapshot, cfg["model"])
    state["calls_today"] += 1

    if usage is not None:
        ctx.claude_budget.record_spend(usage, cfg["model"])
    if decision is None:
        # don't start the cooldown on a failed/unparseable call -- retry
        # next run instead of waiting out a full cooldown for nothing
        return False

    state["last_call_time"] = ctx.now.timestamp()

    fired = False
    reply_results = []
    replied_ids = state.setdefault("replied_tweet_ids", [])
    acct_caps = state.setdefault("account_replies_today", {})
    for r in (decision.get("replies") or [])[: cfg["max_replies_per_call"]]:
        idx = r.get("candidate_index")
        if idx is None or idx < 0 or idx >= len(candidates) or not r.get("text"):
            continue
        candidate = candidates[idx]
        handle = candidate["handle"]
        cap = next(
            (t.get("times_per_day", 1) for t in ctx.config["reply_targets"]["targets"] if t["handle"] == handle),
            1,
        )
        if state["replies_today"] >= cfg["max_replies_per_day"] or acct_caps.get(handle, 0) >= cap:
            continue
        if not ctx.budget.can_spend(has_link=False):
            break

        text = truncate(r["text"], reply_manager_brain.MAX_REPLY_LEN)
        reply_id = ctx.x.reply(text, candidate["tweet_id"])
        status = "sent" if reply_id else "failed"
        reply_results.append({"handle": handle, "text": text, "reasoning": r.get("reasoning", ""), "status": status})
        if reply_id:
            # a reply isn't original content -- never mirror to the public channel
            ctx.budget.record_spend(has_link=False, text=f"Reply to @{handle}: {text}", mirror_to_channel=False)
            replied_ids.append(candidate["tweet_id"])
            acct_caps[handle] = acct_caps.get(handle, 0) + 1
            state["replies_today"] += 1
            fired = True

    state["replied_tweet_ids"] = replied_ids[-500:]
    _send_audit_message(reply_results)
    return fired
