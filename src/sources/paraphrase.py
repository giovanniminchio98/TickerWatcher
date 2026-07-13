"""
Turns an RSS headline+summary into a short first-person paraphrase for the
"JUST IN" post, honoring the "never reproduce article text verbatim" rule.

Two modes:
  - If ANTHROPIC_API_KEY is set, ask Claude Haiku for a genuine one-sentence
    paraphrase (a fraction of a cent per call -- optional upgrade, not
    required for the bot to run).
  - Otherwise, fall back to a mechanical rewrite of the headline (strip the
    publisher's own phrasing/branding, trim length). This is NOT true
    semantic paraphrasing, just a cheap deterministic transform -- good
    enough to avoid verbatim republishing, but the Claude path reads better.
    This is a free-tier limitation worth knowing about (see README).
"""
import logging
import os
import re

logger = logging.getLogger("tickerwatch.paraphrase")

MAX_PARAPHRASE_LEN = 200


def _mechanical_condense(title):
    text = re.sub(r"\s*[-|]\s*[A-Za-z0-9. ]+$", "", title)  # drop trailing " - Publisher Name"
    text = text.strip()
    if len(text) > MAX_PARAPHRASE_LEN:
        text = text[: MAX_PARAPHRASE_LEN - 1].rstrip() + "…"
    return text


def _llm_paraphrase(title, summary):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = (
        "Paraphrase the following news headline/summary in exactly ONE short, factual sentence "
        f"under {MAX_PARAPHRASE_LEN} characters. Do not copy phrasing verbatim. Do not add opinion, "
        "speculation, or any fact not present in the source. Output only the sentence, no quotes.\n\n"
        f"Headline: {title}\nSummary: {summary}"
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    if len(text) > MAX_PARAPHRASE_LEN:
        text = text[: MAX_PARAPHRASE_LEN - 1].rstrip() + "…"
    return text


def paraphrase(title, summary):
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _llm_paraphrase(title, summary)
        except Exception:
            logger.exception("LLM paraphrase failed, falling back to mechanical condense")
    return _mechanical_condense(title)
