"""Visible text for Lain's replies. Numeric reports stay in the timeline."""


def reply_text(value):
    """Pass model speech through; only strip controls and cap length for the chat log."""
    text = "".join(c for c in str(value or "") if c.isprintable() or c == "\n").strip()
    limit = 1600
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
