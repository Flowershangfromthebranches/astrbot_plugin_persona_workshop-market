from __future__ import annotations


def abbreviate(text: str, limit: int = 120) -> str:
    """Collapse whitespace and truncate; this does NOT remove secrets."""

    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."
