MAX_TWEET_LEN = 280


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


def truncate(text, max_len=MAX_TWEET_LEN):
    """Hard truncate as a last-resort safety net; callers should compose
    posts to naturally fit under the limit."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


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
