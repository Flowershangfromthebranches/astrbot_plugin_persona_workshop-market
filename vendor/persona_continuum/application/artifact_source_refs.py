"""Deterministic source-reference hygiene for dimension ResearchArtifacts.

Models routinely copy an evidence id into a source slot with the prefix
swapped (``evu_<hex>`` written as ``src_<hex>``) or cite a source that is not
in the persona at all.  Left alone, one such reference either discarded a whole
dimension (fail-closed normalization) or reached the final LLM audit and failed
the quality gate.  These helpers resolve what is unambiguous and remove the
rest, so a dangling reference can never pass as provenance and never costs a
model call to fix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SOURCE_REF_PATTERN = re.compile(r"\bsrc_[0-9a-f]{8,32}\b")
_EVIDENCE_PREFIXES = ("evu_", "evf_")
# Separators left behind after a reference is removed from free text.
_EMPTY_GROUP = re.compile(r"[（(]\s*[)）]")
_LEADING_SEPARATOR = re.compile(r"([（(])\s*[,，、]\s*")
_TRAILING_SEPARATOR = re.compile(r"\s*[,，、]\s*([)）])")
_REPEATED_SEPARATOR = re.compile(r"([,，、])\s*[,，、]")


def resolve_source_ref(
    reference: str | None,
    *,
    known_sources: set[str],
    evidence_sources: Mapping[str, set[str]],
) -> str | None:
    """Resolve one reference to a known source id, or ``None`` when ambiguous.

    Never guesses: an evidence id resolves only when it maps to exactly one
    known source, and a ``src_<hex>`` alias only when the matching evidence id
    does.
    """

    ref = str(reference or "").strip()
    if not ref:
        return None
    if ref in known_sources:
        return ref
    candidates = {value for value in evidence_sources.get(ref, set()) if value in known_sources}
    if len(candidates) == 1:
        return next(iter(candidates))
    if ref.startswith("src_"):
        suffix = ref.removeprefix("src_")
        aliased = {
            value
            for prefix in _EVIDENCE_PREFIXES
            for value in evidence_sources.get(prefix + suffix, set())
            if value in known_sources
        }
        if len(aliased) == 1:
            return next(iter(aliased))
    return None


def evidence_ids_for_aliases(references: Iterable[str]) -> set[str]:
    """Evidence ids an unknown ``src_<hex>`` reference could be an alias of."""

    ids: set[str] = set()
    for reference in references:
        ref = str(reference or "")
        if ref.startswith("src_"):
            suffix = ref.removeprefix("src_")
            ids.update(prefix + suffix for prefix in _EVIDENCE_PREFIXES)
        elif ref.startswith(_EVIDENCE_PREFIXES):
            ids.add(ref)
    return ids


def unknown_source_refs(value: Any, known_sources: set[str]) -> set[str]:
    """Every ``src_<hex>`` token anywhere in ``value`` that is not a known source."""

    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(
                match for match in SOURCE_REF_PATTERN.findall(node) if match not in known_sources
            )
        elif isinstance(node, Mapping):
            for key, item in node.items():
                walk(key)
                walk(item)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(value)
    return found


def _clean_text(text: str, replacements: Mapping[str, str | None]) -> str:
    def substitute(match: re.Match[str]) -> str:
        ref = match.group(0)
        if ref not in replacements:
            return ref
        return replacements[ref] or ""

    cleaned = SOURCE_REF_PATTERN.sub(substitute, text)
    if cleaned == text:
        return text
    cleaned = _REPEATED_SEPARATOR.sub(r"\1", cleaned)
    cleaned = _LEADING_SEPARATOR.sub(r"\1", cleaned)
    cleaned = _TRAILING_SEPARATOR.sub(r"\1", cleaned)
    cleaned = _EMPTY_GROUP.sub("", cleaned)
    return cleaned


def scrub_source_refs(value: Any, replacements: Mapping[str, str | None]) -> Any:
    """Rewrite or drop the given source tokens throughout a JSON-like value.

    A list element that is exactly a dropped reference is removed (and the
    list de-duplicated); a reference inside free text is replaced in place.
    """

    if isinstance(value, str):
        stripped = value.strip()
        if stripped in replacements:
            return replacements[stripped]
        return _clean_text(value, replacements)
    if isinstance(value, Mapping):
        return {key: scrub_source_refs(item, replacements) for key, item in value.items()}
    if isinstance(value, list | tuple):
        result: list[Any] = []
        seen: set[str] = set()
        for item in value:
            cleaned = scrub_source_refs(item, replacements)
            if cleaned is None and isinstance(item, str):
                continue
            if isinstance(cleaned, str) and SOURCE_REF_PATTERN.fullmatch(cleaned):
                if cleaned in seen:
                    continue
                seen.add(cleaned)
            result.append(cleaned)
        return result
    return value


def sanitize_artifact_source_refs(
    artifact: Mapping[str, Any],
    *,
    known_sources: set[str],
    evidence_sources: Mapping[str, set[str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a cleaned artifact copy plus a report of what changed.

    Claims and memories whose source cannot be resolved are dropped one by
    one; conflicts and extracted components get unknown references rewritten
    to their unambiguous source or removed.  The artifact-level source list is
    rebuilt from what remains.
    """

    data: dict[str, Any] = {key: value for key, value in artifact.items()}
    report: dict[str, Any] = {"remapped": {}, "removed": [], "dropped_entries": 0}

    def resolve(reference: Any) -> str | None:
        return resolve_source_ref(
            str(reference or "") or None,
            known_sources=known_sources,
            evidence_sources=evidence_sources,
        )

    for field in ("claims", "memories"):
        kept: list[Any] = []
        for entry in data.get(field) or []:
            if not isinstance(entry, Mapping):
                continue
            current = entry.get("source_id")
            resolved = resolve(current) if current else None
            if current and resolved is None:
                report["dropped_entries"] += 1
                continue
            if resolved and resolved != current:
                report["remapped"][str(current)] = resolved
                entry = {**entry, "source_id": resolved}
            kept.append(dict(entry))
        data[field] = kept

    embedded = unknown_source_refs(
        [data.get("conflicts"), data.get("extracted_components")], known_sources
    )
    replacements: dict[str, str | None] = {ref: resolve(ref) for ref in sorted(embedded)}
    if replacements:
        data["conflicts"] = scrub_source_refs(data.get("conflicts") or [], replacements)
        data["extracted_components"] = scrub_source_refs(
            data.get("extracted_components") or {}, replacements
        )
        for ref, resolved in replacements.items():
            if resolved is None:
                report["removed"].append(ref)
            else:
                report["remapped"][ref] = resolved

    listed = [resolve(value) for value in data.get("source_ids") or []]
    referenced = {
        str(entry.get("source_id"))
        for field in ("claims", "memories")
        for entry in data.get(field) or []
        if entry.get("source_id")
    }
    data["source_ids"] = sorted({value for value in listed if value} | referenced)
    report["changed"] = bool(
        report["remapped"] or report["removed"] or report["dropped_entries"]
        or data["source_ids"] != sorted(set(artifact.get("source_ids") or []))
    )
    return data, report


__all__ = [
    "SOURCE_REF_PATTERN",
    "evidence_ids_for_aliases",
    "resolve_source_ref",
    "sanitize_artifact_source_refs",
    "scrub_source_refs",
    "unknown_source_refs",
]
