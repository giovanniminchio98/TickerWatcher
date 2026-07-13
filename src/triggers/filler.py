"""
Lowest-priority safety net: fires only if nothing else posted this run, so
the account still posts roughly once per check (hourly by default) instead
of going silent on quiet hours. Picks from config/filler.json's ~100 generic
engagement prompts / evergreen facts without repeating until the whole list
has been used once, then reshuffles.

Bounded by thresholds.filler.max_per_day so it can't quietly eat the whole
monthly budget on its own -- see README for the cost trade-off of "at least
one post per hour."
"""
import logging
import random

from src.formatting import truncate

logger = logging.getLogger("tickerwatch.triggers.filler")


def _next_filler_text(ctx):
    posts = ctx.config["filler"]["posts"]
    state = ctx.state["filler"]
    if not state["shuffled_bag"]:
        bag = list(range(len(posts)))
        random.shuffle(bag)
        state["shuffled_bag"] = bag
    index = state["shuffled_bag"].pop()
    return posts[index]


def run(ctx, higher_priority_fired):
    if higher_priority_fired:
        return False

    state = ctx.state["filler"]
    today_str = ctx.now.strftime("%Y-%m-%d")
    if state["posted_date"] != today_str:
        state["posted_date"] = today_str
        state["posted_count_today"] = 0

    max_per_day = ctx.config["thresholds"]["filler"]["max_per_day"]
    if state["posted_count_today"] >= max_per_day:
        return False

    if not ctx.budget.can_spend(has_link=False):
        return False

    text = truncate(_next_filler_text(ctx))
    tweet_id = ctx.x.post(text)
    if tweet_id:
        ctx.budget.record_spend(has_link=False, text=text)
        state["posted_count_today"] += 1
        return True
    return False
