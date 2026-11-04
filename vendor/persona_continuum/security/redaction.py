"""The single secret-redaction policy for logs, transcripts and API payloads."""

from __future__ import annotations

import re
from typing import Any

SECRET_PLACEHOLDER = "[REDACTED_SECRET]"
KNOWN_SECRET_PLACEHOLDER = "[REDACTED_API_KEY]"

# Provider key shapes, then generic credential assignments.  Every consumer
# (diagnostics, transcripts, error responses) uses this one list.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),  # OpenAI / Anthropic
    re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),  # Google
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{12,}"),  # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}"),
)
_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|authorization)"
    r"(\s*[:=]\s*['\"]?)([^\s'\",;]+)"
)


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    redacted = text
    for secret in extra_secrets or ():
        if secret and len(secret) > 4:
            redacted = redacted.replace(secret, KNOWN_SECRET_PLACEHOLDER)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(SECRET_PLACEHOLDER, redacted)
    return _ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{SECRET_PLACEHOLDER}", redacted
    )


def redact_secrets(value: Any, extra_secrets: list[str] | None = None) -> Any:
    """Redact strings anywhere inside dicts and lists; other values pass through."""

    if isinstance(value, dict):
        return {key: redact_secrets(item, extra_secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, extra_secrets) for item in value]
    if not isinstance(value, str) or not value:
        return value
    return redact_text(value, extra_secrets)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS) or bool(
        _ASSIGNMENT.search(text)
    )
