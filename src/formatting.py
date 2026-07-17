import re

MAX_TWEET_LEN = 280

# A sentence-ending punctuation mark followed by whitespace (space OR a
# newline) or end-of-string -- matches both a plain ". " and our own
# tag+headline+blank-line+explanation post shape's ".\n\n" separator,
# which a plain ". " search would miss entirely.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


def fmt_price(value):
    if value is None:
        return "N/A"
    value = float(value)
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.4f}"


def fmt_pct(value):
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def dot_for_change(value):
    """Green/red dot for a price/pct change, used consistently across every
    post type that shows one (snapshot, whale alerts, price alerts,
    flashback, self-reply) so the visual language stays uniform."""
    if value is None:
        return "⚪"
    return "🟢" if value >= 0 else "🔴"


def fmt_usd_compact(value):
    value = float(value)
    for unit, threshold in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= threshold:
            return f"${value / threshold:,.1f}{unit}"
    return f"${value:,.0f}"


def _last_sentence_end(window):
    """Index of the last complete-sentence-ending punctuation within window,
    or -1 if none found. Ignores a trailing ellipsis itself (that's the
    thing we're trying to cut back past, not a sentence end)."""
    core = window.rstrip()
    if core.endswith("…"):
        core = core[:-1]
    elif core.endswith("..."):
        core = core[:-3]
    matches = list(_SENTENCE_END_RE.finditer(core))
    return matches[-1].start() if matches else -1


def truncate(text, max_len=MAX_TWEET_LEN):
    """Hard truncate as a last-resort safety net; callers should compose
    posts to naturally fit under the limit. Prefers cutting at the last
    complete sentence within the limit (no ellipsis needed, reads as a
    genuine ending) over a flat mid-word/mid-thought chop -- a post should
    never read as truncated, even when this safety net has to fire.

    When no usable sentence boundary exists, falls back to keeping just the
    first paragraph -- the punchy headline before the blank-line separator
    in our tag+headline+blank-line+explanation post shape -- rather than an
    ugly ellipsis cut into the middle of the explanation. The headline alone
    reads as a complete, deliberate post; a random mid-sentence chop into
    the "why it matters" half never does. Only falls all the way back to a
    flat cut+ellipsis (when over budget) if even the headline paragraph
    itself is too long or there's no paragraph break to fall back to at all.

    Also fires when text is already UNDER max_len but ends with a dangling
    "…"/"..." -- confirmed live, twice, that Claude sometimes self-truncates
    mid-sentence while composing to stay under budget, producing a complete
    (sub-limit) string that still reads as cut off. That case never reached
    the length check below at all, so it silently passed through unfixed
    until now: any dangling ellipsis gets trimmed back to the last real
    sentence, however short, since there's no length pressure forcing a
    trade-off in that case."""
    over_budget = len(text) > max_len
    self_truncated = text.rstrip().endswith("…") or text.rstrip().endswith("...")
    if not over_budget and not self_truncated:
        return text

    window = text[:max_len] if over_budget else text
    best_end = _last_sentence_end(window)

    min_keep = max_len * 0.5 if over_budget else 0
    if best_end >= min_keep:
        return text[: best_end + 1].rstrip()

    first_para = text.split("\n\n", 1)[0].rstrip()
    first_para_dangling = first_para.endswith("…") or first_para.endswith("...")
    if first_para != text.rstrip() and len(first_para) <= max_len and not first_para_dangling:
        return first_para

    if over_budget:
        return text[: max_len - 1].rstrip() + "…"
    return text


def thread_parts(text, max_len=MAX_TWEET_LEN):
    """Split long text into thread-sized chunks of <= max_len chars each,
    breaking on whitespace where possible. Used only for scheduled posts
    that might overflow a single tweet (e.g. a long watchlist snapshot)."""
    if len(text) <= max_len:
        return [text]
    words = text.split(" ")
    parts = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_len:
            if current:
                parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
