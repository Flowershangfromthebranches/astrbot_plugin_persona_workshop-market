"""Errors and audit schema shared by persona creation and its audit gate.

Kept apart from ``persona_creation_service`` so the audit gate module can use
them without importing the orchestrator (which would be circular).
"""

from __future__ import annotations

from typing import Any

from persona_continuum.application.job_progress import PersonaFailureCode


class PersonaCreationError(RuntimeError):
    """Base error for fail-closed persona creation jobs."""


def classify_persona_failure_message(message: str) -> str:
    """Classify a pipeline-raised message without importing the agent layer."""

    for prefix, code in (
        ("audit_repair_failed:", PersonaFailureCode.AUDIT_REPAIR_FAILED),
        ("final_quality_gate_failed:", PersonaFailureCode.FINAL_QUALITY_GATE_FAILED),
        ("final_audit_", PersonaFailureCode.FINAL_AUDIT_FAILED),
        ("final_audit_failed", PersonaFailureCode.FINAL_AUDIT_FAILED),
    ):
        if message.startswith(prefix):
            return code.value
    if "_audit_invalid" in message:
        return PersonaFailureCode.FINAL_AUDIT_FAILED.value
    return PersonaFailureCode.PERSONA_CREATION_ERROR.value


class PersonaQualityGateError(PersonaCreationError):
    """Compilation is blocked because a required final audit did not pass.

    Carries an explicit ``failure_code`` so retry policy never has to
    string-match the message.
    """

    def __init__(
        self, message: str, *, failure_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code or classify_persona_failure_message(message)


# One audit issue.  ``dimensions`` is the canonical shape (always list[str]).
# The legacy singular ``dimension`` is still accepted because persisted
# checkpoints and older models emit it, and it may legitimately be a list when
# a finding is cross-dimension.  The Python consumer normalizes both.
FINAL_AUDIT_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "dimension": {"type": ["string", "array"], "items": {"type": "string"}},
        "severity": {"type": "string"},
        "type": {"type": "string"},
        "category": {"type": "string"},
        "reason": {"type": "string"},
        "detail": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
}

# Headroom for the audit envelope (audit_type + instruction + system prompt)
# that surrounds the payload when the transport guard measures the prompt.
_GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES = 4096

# Issue severities that make the named dimension fail the gate.  Models use
# several spellings for the same level, so "major"/"blocker" count too.
AUDIT_BLOCKING_SEVERITIES = frozenset(
    {"repair_required", "high", "critical", "fail", "error", "major", "blocker", "severe"}
)

FINAL_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "dimensions", "issues"],
    "properties": {
        "status": {"type": "string", "enum": ["pass", "repair_required", "fail"]},
        "dimensions": {"type": "object"},
        # Explicitly typed: "array<any>" let downstream code silently assume a
        # field shape that a model was free to violate.
        "issues": {"type": "array", "items": FINAL_AUDIT_ISSUE_SCHEMA},
        "retrieval_source_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}
