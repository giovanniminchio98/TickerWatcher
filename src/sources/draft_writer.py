"""
Writes a short, ready-to-post draft for the Telegram-only content-drafts
pipeline (src/triggers/content_drafts.py). Unlike news_alerts' paraphrase
(a neutral restatement) or reply_writer's reply (reacting to someone else's
post), this is meant to read like a person's own original take -- grounded
only in the given fact, with room for a light observation/angle on top, since
a human reviews and refines every draft before it ever reaches X.

Requires ANTHROPIC_API_KEY -- there's no safe mechanical fallback for
"curated insight" text (unlike news paraphrasing, which can fall back to a
mechanical headline trim), so this simply returns None without it.
"""
import logging
import os

logger = logging.getLogger("tickerwatch.draft_writer")

MAX_DRAFT_LEN = 260


def write_draft(fact):
    """fact is a short plain-language description of something real that
    just happened (a price move, a news item, a stock quote) -- returns a
    ready-to-post draft string, or None if drafting isn't available/fails."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("ANTHROPIC_API_KEY not set, skipping draft generation (no safe fallback)")
        return None

    import anthropic

    prompt = (
        "Draft ONE short, ready-to-post X (Twitter) post about crypto/finance/markets, for a "
        "human to review and post personally -- so it should read like a real person's own "
        "take, not a bot alert. Base it ONLY on the fact below; never invent a number, name, "
        "or event not stated in it. You may add one light, reasonable observation on top of the "
        "fact (why it might matter, what to watch) but never state speculation as if it were "
        "confirmed. No hashtags, no links, at most one emoji if it fits naturally. Keep it under "
        f"{MAX_DRAFT_LEN} characters, one to three sentences.\n\n"
        f"Fact: {fact}\n\n"
        "Output only the post text, nothing else, no quotes."
    )
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip().strip('"')
    except Exception:
        logger.exception("Draft generation via Claude failed")
        return None

    if not text:
        return None
    if len(text) > MAX_DRAFT_LEN:
        text = text[: MAX_DRAFT_LEN - 1].rstrip() + "…"
    return text
