import json
import os
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT_DIR, "state", "state.json")

DEFAULT_STATE = {
    "whale": {
        "last_btc_block_height": None,
        "seen_btc_txids": [],
        "last_eth_block": None,
        "posted_date": None,
        "posted_count_today": 0,
    },
    "news": {"posted_urls": [], "posted_date": None, "posted_count_today": 0},
    "price": {"last_alert_price": {}, "last_alert_time": {}},
    "scheduled_daily": {"last_posted_date": None, "rotate_index": 0},
    "flashback": {"last_posted_date": None},
    "polls": {"last_posted_date": None},
    "self_reply": {"pending": []},
    "retweets": {"last_seen_tweet_id": {}, "resolved_accounts": {}},
    "comment_engagement": {},
    "ai_manager": {
        "last_call_time": None,
        "date": None,
        "calls_today": 0,
        "posts_today": 0,
        "reposts_today": 0,
        "recent_post_texts": [],
        "reposted_tweet_ids": [],
        "account_reposts_today": {},
        "resolved_accounts": {},
        "post_queue": [],
        "posts_since_last_second_part": 0,
    },
    "reply_manager": {
        "last_call_time": None,
        "date": None,
        "calls_today": 0,
        "replies_today": 0,
        "replied_tweet_ids": [],
        "account_replies_today": {},
        "resolved_accounts": {},
    },
    "claude_budget": {"period": None, "usd_used": 0.0, "low_budget_alert_sent_period": None},
    "image_budget": {"period": None, "usd_used": 0.0, "low_budget_alert_sent_period": None},
    "reply_suggestions": {"shown_tweet_ids": [], "resolved_accounts": {}},
    "content_drafts": {
        "last_drafted_time": {},
        "drafted_urls": [],
        "posted_date": None,
        "posted_count_today": 0,
    },
    "filler": {"shuffled_bag": [], "posted_date": None, "posted_count_today": 0},
    "budget": {
        "period": None,
        "posts_used": 0,
        "usd_used": 0.0,
        "daily": {"date": None, "posts_used": 0, "usd_used": 0.0},
        "low_budget_alert_sent_period": None,
    },
    "telegram": {"last_report_date": None},
    "run": {"any_trigger_fired_today": False, "last_run_date": None},
}


def load_state():
    if not os.path.exists(STATE_PATH):
        return json.loads(json.dumps(DEFAULT_STATE))
    with open(STATE_PATH, "r") as f:
        state = json.load(f)
    # backfill any keys added since this state file was written
    for key, default in DEFAULT_STATE.items():
        state.setdefault(key, json.loads(json.dumps(default)))
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(STATE_PATH))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp_path, STATE_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
