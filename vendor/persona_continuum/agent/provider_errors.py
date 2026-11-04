"""Recognise provider error text that says what to do next.

Two shapes matter for long-running work.  A transient provider failure (a
dropped stream, a 5xx, an overloaded backend) succeeds on replay, so a
380k-message job must not end on one.  A tool-schema rejection comes from an
MCP server the user registered with their CLI; every request fails until
that server is disabled, so the message has to name it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_TRANSIENT_MARKERS = (
    "stream was interrupted",
    "stream interrupted",
    "stream closed unexpectedly",
    "unavailable",
    "overloaded",
    "deadline exceeded",
    "deadline_exceeded",
    "internal error",
    "internal server error",
    "bad gateway",
    "gateway timeout",
    "connection reset",
    "econnreset",
    "socket hang up",
    "eligibility check failed (transient",
)
_TRANSIENT_STATUS = re.compile(r"\b(?:code|status|http)[ :=(]*(?:500|502|503|504)\b")
# Errors that say the request itself is wrong never become transient, even if
# the provider's text also mentions an unavailable resource.
_PERMANENT_MARKERS = ("invalid_argument", "invalid argument", "(code 400)", "permission_denied")
_TOOL_SCHEMA = re.compile(
    r"function_declarations\[\d+\]\.parameters\.properties\[([^\]]+)\]", re.IGNORECASE
)


def is_transient_provider_error(text: str) -> bool:
    lowered = (text or "").casefold()
    if any(marker in lowered for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSIENT_MARKERS) or bool(
        _TRANSIENT_STATUS.search(lowered)
    )


def rejected_tool_parameter(text: str) -> str | None:
    """The parameter name when the provider rejected a tool declaration."""

    match = _TOOL_SCHEMA.search(text or "")
    return match.group(1) if match else None


def mcp_servers_declaring(parameter: str, schema_dirs: list[Path]) -> list[str]:
    """MCP servers whose cached tool schemas declare ``parameter`` with an enum.

    agy caches each server's tool schemas as ``<dir>/<server>/<tool>.json``.
    Only names are returned; schema contents never leave this function.
    """

    servers: set[str] = set()
    for root in schema_dirs:
        if not root.is_dir():
            continue
        for path in root.glob("*/*.json"):
            try:
                if path.stat().st_size > 512_000:
                    continue
                schema = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _declares_enum(schema, parameter):
                servers.add(path.parent.name)
    return sorted(servers)


def _declares_enum(node: object, parameter: str) -> bool:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            declared = properties.get(parameter)
            if isinstance(declared, dict) and "enum" in declared:
                return True
        return any(_declares_enum(value, parameter) for value in node.values())
    if isinstance(node, list):
        return any(_declares_enum(value, parameter) for value in node)
    return False


__all__ = [
    "is_transient_provider_error",
    "mcp_servers_declaring",
    "rejected_tool_parameter",
]
