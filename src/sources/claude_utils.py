"""
Shared helper for every Claude call site in this codebase (ai_manager_brain,
reply_writer, draft_writer, paraphrase). A response's `content` list isn't
guaranteed to have the answer at index 0 -- a model can emit a ThinkingBlock
(or, in principle, other non-text blocks) first, so `resp.content[0].text`
is not safe to assume. Confirmed live: claude-sonnet-5 returned a
ThinkingBlock at content[0] with no `thinking` param even requested, which
crashed ai_manager_brain's naive content[0].text access in production.
"""


def extract_text(resp):
    """Returns the first text block's stripped content, or "" if none."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return ""
