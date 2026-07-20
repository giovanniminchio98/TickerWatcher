"""
Turns an RSS headline+summary into a short first-person paraphrase for the
"JUST IN" post, honoring the "never reproduce article text verbatim" rule,
plus a plain-language explanation of what it means -- posted as a mandatory
reply by news_alerts.py, same "explain the news, not just headline it"
pattern as ai_manager's mandatory second_part.

Two modes:
  - If ANTHROPIC_API_KEY is set, ask Claude Haiku for a genuine one-sentence
    paraphrase, a bullish/bearish/neutral sentiment tag, and a one-sentence
    plain-language explanation, all in the same call (a fraction of a cent
    total -- optional upgrade, not required for the bot to run). The
    sentiment tag picks which themed trend image (assets/trend_*.png) gets
    attached to the post, matching the red/green chart-snippet style other
    crypto news accounts use -- see news_alerts.py.
  - Otherwise, fall back to a mechanical rewrite of the headline (strip the
    publisher's own phrasing/branding, trim length) with no sentiment tag and
    no explanation, so no image and no reply get attached. This is NOT true
    semantic paraphrasing, just a cheap deterministic transform -- good
    enough to avoid verbatim republishing, but the Claude path reads better
    and is the only path that can safely judge sentiment or write a real
    explanation. This is a free-tier limitation worth knowing about (see
    README).
"""
import logging
import os
import re

from src import ops_alerts
from src.sources.claude_utils import extract_text

logger = logging.getLogger("tickerwatch.paraphrase")

MAX_PARAPHRASE_LEN = 200
MAX_EXPLANATION_LEN = 250
VALID_SENTIMENTS = {"up", "down", "neutral"}


def _mechanical_condense(title):
    # drop a trailing " - Publisher Name" / " | Publisher Name" suffix -- requires
    # whitespace on both sides of the separator so hyphenated words in the title
    # itself (e.g. "third-party") are never mistaken for a publisher suffix
    text = re.sub(r"\s+[-|]\s+[A-Za-z0-9. ]{1,40}$", "", title)
    text = text.strip()
    if len(text) > MAX_PARAPHRASE_LEN:
        text = text[: MAX_PARAPHRASE_LEN - 1].rstrip() + "…"
    return text


def _llm_paraphrase_with_sentiment(title, summary):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = (
        "Paraphrase the following news headline/summary in exactly ONE short, factual sentence "
        f"under {MAX_PARAPHRASE_LEN} characters. Do not copy phrasing verbatim. Do not add opinion, "
        "speculation, or any fact not present in the source.\n\n"
        "Then, on a second line, classify the news as one word: 'up' if it's bullish/positive for "
        "the relevant market, 'down' if it's bearish/negative, or 'neutral' if it's neither "
        "(purely factual/no clear market direction). Only use up/down if the source itself implies "
        "a direction -- don't guess.\n\n"
        "Then, on a third line, write ONE short, plain-language sentence explaining what this "
        f"actually means or why it matters, under {MAX_EXPLANATION_LEN} characters -- written so "
        "someone with no background understands it. This gets posted as a reply right under the "
        "headline, so never just restate the headline in different words, never add a fact not "
        "present in the source, and never speculate beyond what's given.\n\n"
        f"Headline: {title}\nSummary: {summary}\n\n"
        "Output exactly three lines: the paraphrase sentence, the sentiment word, then the "
        "explanation sentence. No quotes, no labels, nothing else."
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = extract_text(resp)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    text = lines[0] if lines else raw
    sentiment = lines[1].lower() if len(lines) > 1 else None
    if sentiment not in VALID_SENTIMENTS:
        sentiment = None
    explanation = lines[2] if len(lines) > 2 else None
    if len(text) > MAX_PARAPHRASE_LEN:
        text = text[: MAX_PARAPHRASE_LEN - 1].rstrip() + "…"
    if explanation and len(explanation) > MAX_EXPLANATION_LEN:
        explanation = explanation[: MAX_EXPLANATION_LEN - 1].rstrip() + "…"
    return text, sentiment, explanation


def paraphrase_with_sentiment(title, summary):
    """Returns (text, sentiment, explanation) where sentiment is
    'up'/'down'/'neutral'/None and explanation is a plain-language "what
    this means" sentence, or None on the mechanical fallback path (no LLM
    available to write one)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _llm_paraphrase_with_sentiment(title, summary)
        except Exception as e:
            logger.exception("LLM paraphrase failed, falling back to mechanical condense")
            ops_alerts.notify_claude_failure(f"paraphrase: {e}")
    return _mechanical_condense(title), None, None


def paraphrase(title, summary):
    text, _sentiment, _explanation = paraphrase_with_sentiment(title, summary)
    return text
