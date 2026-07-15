"""
Generates a post's accompanying image via OpenAI's Images API (DALL-E 3),
opt-in via OPENAI_API_KEY presence -- same "no safe fallback, just skip"
reasoning as every other optional API key in this codebase. Claude itself
cannot generate images (text/vision-in, text-out only), so the split is:
Claude (ai_manager_brain) writes the descriptive image_prompt as part of
its normal JSON decision -- no extra Claude call needed -- and this module
turns that prompt into an actual image via a separate provider.

Without OPENAI_API_KEY (or on any generation failure), returns None and the
caller falls back to attaching a real link instead (see ai_manager.py) --
the "always image or link" rule never leaves a post with neither.

Cost is billed by src.image_budget.ImageBudget from the fixed
(model, size, quality) tuple actually requested, not estimated -- OpenAI's
Images API doesn't return per-call usage the way Claude's does.
"""
import logging
import os

logger = logging.getLogger("tickerwatch.image_gen")

MODEL = "dall-e-3"
SIZE = "1024x1024"
QUALITY = "standard"


def generate_post_image(prompt):
    """Returns raw PNG/JPEG bytes on success, or None if OPENAI_API_KEY is
    unset or generation fails for any reason (network, moderation refusal,
    rate limit, etc.) -- never raises, since a missing image should always
    degrade to the link fallback, not break the run."""
    if not os.environ.get("OPENAI_API_KEY"):
        logger.info("OPENAI_API_KEY not set, skipping image generation")
        return None

    import requests
    from openai import OpenAI

    try:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.images.generate(
            model=MODEL,
            prompt=prompt,
            size=SIZE,
            quality=QUALITY,
            n=1,
        )
        image_url = resp.data[0].url
        image_resp = requests.get(image_url, timeout=30)
        image_resp.raise_for_status()
        return image_resp.content
    except Exception:
        logger.exception("Image generation failed for prompt: %r", prompt)
        return None
