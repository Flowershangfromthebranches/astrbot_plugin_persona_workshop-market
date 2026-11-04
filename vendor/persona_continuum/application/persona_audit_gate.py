"""Final quality gate for persona creation: global audits and targeted repair.

Split out of ``persona_creation_service``.  ``PersonaAuditGateMixin`` is mixed
into ``PersonaCreationOrchestrator``; the attributes and helpers it relies on
are declared under ``TYPE_CHECKING`` only, so at runtime they resolve to the
orchestrator's own implementations.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import AgentTurn, EffectiveModelCapabilities
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability
from persona_continuum.agent.response_collector import (
    AgentOutputError,
    AgentRuntimeError,
    PromptTransportLimitExceededError,
    sanitize_diagnostic,
)
from persona_continuum.agent.structured_output import StructuredOutputSchemaError
from persona_continuum.application.artifact_source_refs import (
    evidence_ids_for_aliases,
    sanitize_artifact_source_refs,
    unknown_source_refs,
)
from persona_continuum.application.audit_issues import (
    issues_for_dimension,
    normalize_audit_issue,
    normalize_audit_issue_dimensions,
)
from persona_continuum.application.job_progress import (
    JobPauseRequested,
    PersonaFailureCode,
    WorkerState,
)
from persona_continuum.application.material_intelligence import PersonaEvidenceIndex
from persona_continuum.application.persona_creation_contracts import (
    _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES,
    AUDIT_BLOCKING_SEVERITIES,
    FINAL_AUDIT_SCHEMA,
    PersonaCreationError,
    PersonaQualityGateError,
)
from persona_continuum.compiler.schemas import REQUIRED_DIMENSIONS, ResearchArtifact
from persona_continuum.domain.provenance import CHARACTER_VISIBLE, normalise_material_scope
from persona_continuum.numeric import safe_int
from persona_continuum.security.validation import CodedError
from persona_continuum.utils import new_id

if TYPE_CHECKING:
    from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
    from persona_continuum.application.container import PersonaContinuum
    from persona_continuum.application.material_intelligence import MaterialIntelligenceService
    from persona_continuum.application.persona_creation_service import PersonaCreationJob
    from persona_continuum.application.profile_library_service import ProfileLibraryService

_PauseRequested = JobPauseRequested


class PersonaAuditGateMixin:
    """Global audits, deterministic checks and bounded per-dimension repair."""

    if TYPE_CHECKING:
        continuum: PersonaContinuum
        profile_library: ProfileLibraryService | None
        material_intelligence: MaterialIntelligenceService | None
        runtime_executor: AgentRuntimeExecutor | Any

        async def _agent_json(
            self,
            job: PersonaCreationJob,
            *,
            user_message: str,
            system_prompt: str,
            participant_id: str = "persona_creation",
            phase: str | None = None,
            schema: Any | None = None,
        ) -> Any:
            ...

        async def _drop_job_session(self, job_id: str, participant_id: str) -> None:
            ...

        def _effective_model_capabilities(
            self, job: PersonaCreationJob
        ) -> EffectiveModelCapabilities:
            ...

        async def _emit(self, job: PersonaCreationJob, event_type: str, **payload: Any) -> None:
            ...

        def _latest_dimension_artifacts(self, job: PersonaCreationJob) -> dict[str, dict[str, Any]]:
            ...

        def _raise_if_pause_requested(self, job: PersonaCreationJob | str) -> None:
            ...

        async def _run_dimension_extraction(
            self,
            job: PersonaCreationJob,
            *,
            dimension: str,
            evidence_items: list[dict[str, Any]],
            participant_id: str,
            mode: str,
            source_by_id: dict[str, Any],
            context_manager: Any,
            phase: str = "dimension_extraction",
            repair_context: dict[str, Any] | None = None,
            carried_source_ids: set[str] | None = None,
        ) -> ResearchArtifact:
            ...

        def _save(self, job: PersonaCreationJob) -> None:
            ...

    async def _run_global_audits(self, job: PersonaCreationJob) -> None:
        """Run fact/provenance and cross-dimension audits over the ledger.

        Raw webpages are not reread eight times.  Both audits receive the
        compact Evidence Ledger plus the latest eight artifacts.

        Order matters for the repair budget:

        1. Source references are cleaned deterministically (no model call).
        2. Structural gaps (missing artifact, unsourced claims) are repaired
           before any model audit and do not consume the audit repair budget,
           so the model audit always judges a complete artifact set.
        3. A dimension whose verdict was ``pass`` keeps it while its artifact
           is unchanged; only repaired dimensions are judged again.  Re-judging
           all eight every round let auditor variance fail a dimension that had
           already passed with no repair attempt left.
        """

        if not bool(getattr(self.continuum.config, "persona_global_audit", True)):
            return
        if not job.persona_id:
            raise PersonaQualityGateError(
                "final_audit_persona_missing",
                failure_code=PersonaFailureCode.FINAL_AUDIT_FAILED.value,
            )

        attempts = (
            safe_int(
                getattr(self.continuum.config, "persona_audit_repair_attempts", 2),
                default=2,
                minimum=0,
                maximum=3,
            )
            or 0
        )
        local_material_mode = job.creation_mode != "public_research" or str(
            job.job_config.get("enrichment_input_mode") or ""
        ) in {"local_materials", "hybrid"}
        audit_history: list[dict[str, Any]] = []
        passed_fingerprints: dict[str, str] = {}
        recorded = False

        def record(status: str, **extra: Any) -> None:
            nonlocal recorded
            job.job_config["final_global_audit"] = {
                "status": status,
                "attempts": audit_history,
                **extra,
            }
            recorded = True
            self._save(job)

        job.progress.update_stage("final_audit", label="正在进行终审", percent=94)
        self._save(job)
        try:
            for structural_round in range(attempts):
                self._raise_if_pause_requested(job.id)
                artifacts = await self._sanitize_dimension_artifacts(job)
                structural = self._deterministic_audit_failures(job, artifacts)
                if not structural:
                    break
                audit_history.append(
                    {"attempt": f"structural_{structural_round}", "deterministic": structural}
                )
                self._save(job)
                job.progress.update_stage(
                    "final_audit_repair", label="正在补齐缺失的维度", percent=94
                )
                await self._repair_audit_dimensions(
                    job,
                    set(structural),
                    attempt=structural_round + 1,
                    audit_results={
                        "structural": {
                            "issues": [
                                {"dimension": dimension, "reason": reason, "severity": "high"}
                                for dimension, reason in structural.items()
                            ]
                        }
                    },
                )

            for attempt in range(attempts + 1):
                # Atomic boundary: every audit pass is new model work.
                self._raise_if_pause_requested(job.id)
                artifacts = await self._sanitize_dimension_artifacts(job)
                structural = self._deterministic_audit_failures(job, artifacts)
                fingerprints = {
                    dimension: self.continuum.compilation._canonical_artifact_sha256(artifact)
                    for dimension, artifact in artifacts.items()
                }
                carried = {
                    dimension
                    for dimension, fingerprint in passed_fingerprints.items()
                    if fingerprints.get(dimension) == fingerprint
                }
                results: dict[str, dict[str, Any]] = {}
                if len(carried) < len(REQUIRED_DIMENSIONS):
                    # Model context and transport are independent limits; even
                    # a streaming adapter may have an unknown/small model window.
                    adapter = self.continuum.agent_registry.get_adapter(job.agent_id)
                    transport = (
                        resolve_prompt_transport_capability(adapter)
                        if adapter is not None
                        else None
                    )
                    # Ledger ranking and token planning are CPU-bound on a
                    # large corpus; build the payload off the event loop.
                    payload = await asyncio.to_thread(
                        self._global_audit_payload,
                        job,
                        artifacts,
                        transport_safe_bytes=(
                            transport.safe_prompt_bytes if transport is not None else None
                        ),
                        model=self._effective_model_capabilities(job),
                    )
                    if carried:
                        payload["already_passed_unchanged_dimensions"] = sorted(carried)
                    evidence_result, consistency_result = await asyncio.gather(
                        self._run_final_audit_pass(job, "evidence", payload, attempt=attempt),
                        self._run_final_audit_pass(
                            job, "consistency", payload, attempt=attempt
                        ),
                    )
                    results = {"evidence": evidence_result, "consistency": consistency_result}
                failing = (
                    self._audit_failing_dimensions(
                        results, preserve_documented_source_gaps=local_material_mode
                    )
                    - carried
                ) | set(structural)
                global_blocking = (
                    [] if local_material_mode else self._audit_blocking_global_issues(results)
                )
                audit_history.append(
                    {
                        "attempt": attempt,
                        **results,
                        "carried_pass": sorted(carried),
                        "deterministic": structural,
                        "failing_dimensions": sorted(failing),
                        "global_blocking_issues": global_blocking,
                    }
                )
                for dimension, fingerprint in fingerprints.items():
                    if dimension not in failing:
                        passed_fingerprints[dimension] = fingerprint
                if not failing and not global_blocking:
                    record("pass")
                    await self._emit(job, "persona_final_audit_passed", attempts=attempt + 1)
                    return
                if attempt >= attempts:
                    labels = sorted(failing) or ["global"]
                    record("fail", failing_dimensions=sorted(failing))
                    raise PersonaQualityGateError(
                        "final_quality_gate_failed:" + ",".join(labels),
                        failure_code=PersonaFailureCode.FINAL_QUALITY_GATE_FAILED.value,
                    )
                self._save(job)
                if not failing:
                    # Only cross-cutting findings: nothing dimension-scoped to
                    # repair, so the next attempt is a fresh judgement.
                    continue
                # Another atomic boundary before the bounded repair pass.
                self._raise_if_pause_requested(job.id)
                job.progress.update_stage(
                    "final_audit_repair", label="正在修复终审指出的问题", percent=95
                )
                await self._repair_audit_dimensions(
                    job,
                    failing,
                    attempt=attempt + 1,
                    audit_results=results,
                )
        except BaseException as exc:
            # The failing attempt's verdicts are the diagnosis; keep them even
            # when a repair or an audit call raised mid-loop.  A pause is not
            # an audit outcome: the resumed run re-audits from scratch.
            if not recorded and not isinstance(exc, _PauseRequested):
                with contextlib.suppress(Exception):
                    record("error")
            raise

    @staticmethod
    def _audit_blocking_global_issues(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Blocking findings that name no dimension (cross-cutting defects)."""

        blocking: list[dict[str, Any]] = []
        for result in results.values():
            for issue in result.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                if any(
                    candidate in REQUIRED_DIMENSIONS
                    for candidate in normalize_audit_issue_dimensions(issue)
                ):
                    continue
                if str(issue.get("severity") or "").casefold() in AUDIT_BLOCKING_SEVERITIES:
                    blocking.append(dict(issue))
        return blocking

    async def _sanitize_dimension_artifacts(
        self, job: PersonaCreationJob
    ) -> dict[str, dict[str, Any]]:
        """Clean dangling source references and persist any cleaned artifact.

        Returns the latest artifacts after cleaning.  An artifact that would
        no longer validate is left untouched for the structural audit to flag.
        """

        artifacts = self._latest_dimension_artifacts(job)
        if not job.compilation_task_id or not artifacts:
            return artifacts
        # Any source the persona owns is real provenance (an enrichment run
        # may cite sources from earlier runs); only foreign ids are cleaned.
        known_sources = set(job.source_ids)
        if job.persona_id:
            with contextlib.suppress(Exception):
                known_sources.update(
                    source.id for source in self.continuum.personas.get_sources(job.persona_id)
                )
        suspects: set[str] = set()
        for artifact in artifacts.values():
            for field in ("claims", "memories"):
                for entry in artifact.get(field) or []:
                    if isinstance(entry, dict):
                        source_id = str(entry.get("source_id") or "")
                        if source_id and source_id not in known_sources:
                            suspects.add(source_id)
            suspects.update(
                unknown_source_refs(
                    [artifact.get("conflicts"), artifact.get("extracted_components")],
                    known_sources,
                )
            )
        if not suspects:
            return artifacts
        evidence_sources: dict[str, set[str]] = {}
        if self.material_intelligence is not None and job.persona_id:
            index = self.material_intelligence.get_index(job.persona_id)
            evidence_sources = await asyncio.to_thread(
                index.source_ids_for_evidence, evidence_ids_for_aliases(suspects)
            )
        compilation_service = self.continuum.compilation
        cleaned_artifacts = dict(artifacts)
        for dimension, artifact in artifacts.items():
            cleaned, report = sanitize_artifact_source_refs(
                artifact,
                known_sources=known_sources,
                evidence_sources=evidence_sources,
            )
            if not report.get("changed"):
                continue
            try:
                payload = ResearchArtifact.model_validate(cleaned).model_dump(mode="json")
            except ValueError:
                continue
            payload["artifact_id"] = new_id("art")
            payload["artifact_hash"] = compilation_service._canonical_artifact_sha256(payload)
            try:
                compilation_service.submit_research_artifact(job.compilation_task_id, payload)
            except Exception:
                continue
            cleaned_artifacts[dimension] = payload
            await self._emit(
                job,
                "persona_artifact_sources_sanitized",
                dimension=dimension,
                remapped=len(report.get("remapped") or {}),
                removed=len(report.get("removed") or []),
                dropped_entries=report.get("dropped_entries", 0),
            )
        return cleaned_artifacts

    def _deterministic_audit_failures(
        self, job: PersonaCreationJob, artifacts: dict[str, dict[str, Any]]
    ) -> dict[str, str]:
        known_sources = set(job.source_ids)
        local_material_mode = job.creation_mode != "public_research" or str(
            job.job_config.get("enrichment_input_mode") or ""
        ) in {"local_materials", "hybrid"}
        failures: dict[str, str] = {}
        for dimension in REQUIRED_DIMENSIONS:
            artifact = artifacts.get(dimension)
            if artifact is None:
                failures[dimension] = "missing_artifact"
                continue
            claims = [item for item in artifact.get("claims") or [] if isinstance(item, dict)]
            if not claims:
                uncertainty = artifact.get("uncertainty") or {}
                documented_gap = isinstance(uncertainty, dict) and bool(
                    uncertainty.get("notes")
                    or uncertainty.get("gaps")
                    or uncertainty.get("reason")
                    or uncertainty.get("missing_evidence")
                    or uncertainty.get("no_extractable_content") is True
                    or uncertainty.get("extractable_content_present") is False
                    or (
                        uncertainty.get("empty_content") is True
                        and (uncertainty.get("summary") or uncertainty.get("note"))
                    )
                    or (
                        uncertainty.get("repair_outcome") == "unresolved_source_deficit"
                        and (uncertainty.get("summary") or uncertainty.get("note"))
                    )
                    or (
                        uncertainty.get("missing_information")
                        and (
                            uncertainty.get("summary")
                            or uncertainty.get("extraction_notes")
                            or uncertainty.get("repair_note")
                        )
                    )
                )
                if not (local_material_mode and documented_gap):
                    failures[dimension] = "missing_claims"
                continue
            if any(
                not str(claim.get("source_id") or "")
                or str(claim.get("source_id")) not in known_sources
                for claim in claims
            ):
                failures[dimension] = "invalid_claim_source_link"
        return failures

    def _global_audit_payload(
        self,
        job: PersonaCreationJob,
        artifacts: dict[str, dict[str, Any]],
        *,
        transport_safe_bytes: int | None = None,
        model: EffectiveModelCapabilities | None = None,
    ) -> dict[str, Any]:
        """Compact against both limits, including instructions and output schema."""

        config_ledger_limit = (
            safe_int(
                getattr(self.continuum.config, "persona_global_audit_ledger_items", 32),
                default=32,
                minimum=8,
            )
            or 32
        )
        config_claims_limit = (
            safe_int(
                getattr(
                    self.continuum.config,
                    "persona_global_audit_claims_per_dimension",
                    8,
                ),
                default=8,
                minimum=2,
            )
            or 8
        )
        # Every evidence id retained in the compact artifact is a verification
        # target of the linkage audit.  References belonging to claims removed
        # by compaction are intentionally omitted as well: backfilling those
        # hidden references cannot help the model verify a claim and can exceed
        # an ARGV transport ceiling by tens of kilobytes.
        compaction_levels = (
            (config_ledger_limit, config_claims_limit, 1.0),
            (max(8, config_ledger_limit // 2), max(2, config_claims_limit // 2), 0.5),
            (8, 2, 0.3),
            (4, 1, 0.2),
        )
        payload: dict[str, Any] = {}
        context_manager = getattr(
            self.runtime_executor, "context_budget_manager", AgentContextBudgetManager()
        )
        evidence_index = (
            self.material_intelligence.get_index(job.persona_id)
            if self.material_intelligence is not None and job.persona_id
            else None
        )
        for ledger_limit, claims_limit, text_scale in compaction_levels:
            payload = self._build_audit_payload(
                job,
                artifacts,
                evidence_index=evidence_index,
                ledger_limit=ledger_limit,
                claims_limit=claims_limit,
                text_scale=text_scale,
            )
            turns = [self._final_audit_turn(kind, payload) for kind in ("evidence", "consistency")]
            budgets = [
                context_manager.plan(
                    turn, model=model, phase=f"final_{kind}_audit", enforce=False
                )
                for kind, turn in zip(("evidence", "consistency"), turns, strict=True)
            ]
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            transport_fits = transport_safe_bytes is None or (
                len(encoded) + _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES
                <= int(transport_safe_bytes)
            )
            if transport_fits and all(budget.within_budget for budget in budgets):
                return payload
        # Do not dispatch an oversized last compaction level as if it fitted.
        for kind, turn in zip(("evidence", "consistency"), turns, strict=True):
            context_manager.plan(turn, model=model, phase=f"final_{kind}_audit", enforce=True)
        raise PromptTransportLimitExceededError(
            "Final audit payload exceeds the prompt transport budget",
            phase="final_evidence_audit",
            diagnostics={
                "transport_safe_prompt_bytes": transport_safe_bytes,
                "estimated_prompt_bytes": len(encoded) + _GLOBAL_AUDIT_ENVELOPE_HEADROOM_BYTES,
            },
        )

    def _build_audit_payload(
        self,
        job: PersonaCreationJob,
        artifacts: dict[str, dict[str, Any]],
        *,
        evidence_index: PersonaEvidenceIndex | None = None,
        ledger_limit: int,
        claims_limit: int,
        text_scale: float,
    ) -> dict[str, Any]:
        ledger_text_limit = self._scaled_audit_limit(280, text_scale)
        ledger: list[dict[str, Any]] = []
        contradictions: list[dict[str, Any]] = []
        compact_artifacts = {
            dimension: self._compact_audit_artifact(
                artifact, claims_limit=claims_limit, text_scale=text_scale
            )
            for dimension, artifact in artifacts.items()
        }
        if evidence_index is None and self.material_intelligence is not None and job.persona_id:
            evidence_index = self.material_intelligence.get_index(job.persona_id)
        if evidence_index is not None:
            index = evidence_index
            ledger = [
                {
                    "id": item.get("id"),
                    "text": self._audit_text(item.get("text"), ledger_text_limit),
                    "source_ids": item.get("source_ids") or [],
                    **self._compact_audit_references(
                        item.get("evidence_ids") or [], text_scale=text_scale
                    ),
                    "dimension_scores": item.get("dimension_scores") or {},
                    "intelligence": self._compact_audit_intelligence(
                        item.get("intelligence"), text_scale
                    ),
                    "verbatim_samples": [
                        self._audit_text(value, self._scaled_audit_limit(180, text_scale))
                        for value in (item.get("verbatim_samples") or [])[:2]
                    ],
                }
                for item in index.retrieve(top_k=ledger_limit, diversity=True)
            ]
            contradictions = [
                {
                    "type": item.contradiction_type,
                    "evidence_ids": item.evidence_ids,
                    "summary": self._audit_text(
                        item.summary, self._scaled_audit_limit(320, text_scale)
                    ),
                }
                for item in index.get_contradictions()[:16]
            ]
            referenced_ids = {
                match
                for value in (compact_artifacts, contradictions)
                for match in re.findall(
                    r"\b(?:evu|evf)_[0-9a-f]+\b",
                    json.dumps(value, ensure_ascii=False, default=str),
                )
            }
            included_ids = {
                str(item.get("id") or "") for item in ledger
            } | {
                str(evidence_id)
                for item in ledger
                for evidence_id in item.get("evidence_ids") or []
            }
            missing_ids = referenced_ids - included_ids
            if missing_ids:
                unit_by_id, fused_by_id = index.lookup_semantic(missing_ids)
                for evidence_id in sorted(missing_ids):
                    unit = unit_by_id.get(evidence_id)
                    if unit is not None:
                        ledger.append(
                            {
                                "id": unit.id,
                                "text": self._audit_text(unit.text, ledger_text_limit),
                                "source_ids": [unit.source_id],
                                "dimension_scores": unit.dimension_scores,
                                "referenced_context": True,
                            }
                        )
                        continue
                    # Fused entries live in their own table and only reach the
                    # ledger through the ranked retrieve() window.  A referenced
                    # evf_* id outside that window must still be backfilled, or
                    # the linkage audit can never verify it and the dimension
                    # fails every repair round.
                    fused_item = fused_by_id.get(evidence_id)
                    if fused_item is not None:
                        ledger.append(
                            {
                                "id": fused_item.id,
                                "text": self._audit_text(
                                    fused_item.canonical_claim, ledger_text_limit
                                ),
                                "source_ids": fused_item.source_ids,
                                "dimension_scores": fused_item.dimension_scores,
                                "referenced_context": True,
                            }
                        )
        return {
            "persona_id": job.persona_id,
            "source_ids": sorted(set(job.source_ids)),
            "evidence_ledger": ledger,
            "contradiction_graph": contradictions,
            "dimension_artifacts": compact_artifacts,
            "required_dimensions": list(REQUIRED_DIMENSIONS),
        }

    @staticmethod
    def _scaled_audit_limit(base: int, scale: float) -> int:
        return max(40, int(base * scale))

    @staticmethod
    def _compact_audit_references(
        evidence_ids: list[str], *, text_scale: float
    ) -> dict[str, Any]:
        # A fused record can reference thousands of messages. Its stable id
        # resolves the complete local chain; the audit receives a labelled sample.
        limit = max(2, int(8 * text_scale))
        sampled = evidence_ids[:limit]
        return {
            "evidence_ids": sampled,
            "evidence_id_count": len(evidence_ids),
            "omitted_evidence_id_count": max(0, len(evidence_ids) - len(sampled)),
        }

    @staticmethod
    def _audit_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

    @classmethod
    def _compact_audit_intelligence(
        cls, value: Any, text_scale: float = 1.0
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        compact: dict[str, Any] = {}
        limit = cls._scaled_audit_limit(180, text_scale)
        for key in (
            "claims",
            "events",
            "dates",
            "entities",
            "quotes",
            "contradictions",
            "negative_evidence",
            "uncertainties",
        ):
            raw = value.get(key)
            items = raw if isinstance(raw, list) else [raw] if raw else []
            compact[key] = [
                cls._audit_text(
                    json.dumps(item, ensure_ascii=False, default=str)
                    if isinstance(item, (dict, list))
                    else item,
                    limit,
                )
                for item in items[:2]
            ]
        return {key: items for key, items in compact.items() if items}

    @classmethod
    def _compact_audit_artifact(
        cls,
        artifact: dict[str, Any],
        *,
        claims_limit: int,
        text_scale: float = 1.0,
    ) -> dict[str, Any]:
        claims = []
        claim_limit = cls._scaled_audit_limit(220, text_scale)
        evidence_link_limit = max(1, min(4, int(4 * text_scale)))
        for raw in artifact.get("claims") or []:
            if not isinstance(raw, dict):
                continue
            raw_metadata = raw.get("metadata")
            metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_evidence_ids = raw.get("evidence_ids") or metadata.get("evidence_ids") or []
            if isinstance(raw_evidence_ids, str):
                raw_evidence_ids = [raw_evidence_ids]
            compact_claim = {
                "content": cls._audit_text(raw.get("content"), claim_limit),
                "source_id": raw.get("source_id"),
                "claim_type": raw.get("claim_type"),
                "confidence": raw.get("confidence"),
            }
            evidence_ids = [
                str(value)
                for value in raw_evidence_ids
                if str(value).strip()
            ][:evidence_link_limit]
            if evidence_ids:
                compact_claim["evidence_ids"] = evidence_ids
            claims.append(compact_claim)
            if len(claims) >= claims_limit:
                break
        components = json.dumps(
            artifact.get("extracted_components") or {},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        conflict_limit = cls._scaled_audit_limit(220, text_scale)
        nested_item_limit = max(2, min(8, int(8 * text_scale)))
        return {
            "dimension": artifact.get("dimension"),
            "source_ids": artifact.get("source_ids") or [],
            "claims": claims,
            "conflicts": [
                cls._compact_audit_value(
                    item, conflict_limit, max_items=nested_item_limit
                )
                for item in (artifact.get("conflicts") or [])[:4]
            ],
            "uncertainty": cls._compact_audit_value(
                artifact.get("uncertainty") or {}, conflict_limit, max_items=nested_item_limit
            ),
            "component_summary": cls._audit_text(
                components, cls._scaled_audit_limit(800, text_scale)
            ),
        }

    @classmethod
    def _compact_audit_value(
        cls, value: Any, limit: int, *, max_items: int = 8
    ) -> Any:
        """Keep structured audit fields structured while bounding their text."""

        if isinstance(value, dict):
            return {
                str(key): cls._compact_audit_value(
                    item, limit, max_items=max_items
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._compact_audit_value(item, limit, max_items=max_items)
                for item in value[:max_items]
            ]
        return cls._audit_text(value, limit) if isinstance(value, str) else value

    @staticmethod
    def _final_audit_turn(audit_type: str, payload: dict[str, Any]) -> AgentTurn:
        if audit_type == "evidence":
            instruction = (
                "检查事实准确性、claim/source linkage、时间线、冲突、negative evidence、"
                "uncertainty 与错误归因。特别检查正反自相矛盾：严禁出现同一实体/设定"
                "在某些条目中列为真实事实，而在另一些条目中断言‘官方不存在该设定、属同人二创’"
                "的无根据反向辟谣（例如一面断言是某家族继承人，一面宣称官方不存在该家族）；"
                "严禁混入现实配音演员（CV）争议、游戏运营事故、泛写作教程等场外噪音；"
                "发现自相矛盾或场外污染必须判 repair_required。"
                "每个 required dimension 必须给出 pass/repair_required/fail。"
                "资料未覆盖某维度时，只要 artifact 如实留空或明确记录 uncertainty 且没有补写，"
                "就应判 pass 并把缺口记为 warning/info；"
                "只有 artifact 自身可修复的错误才判 repair_required。"
            )
        else:
            instruction = (
                "检查八维完整性、跨维矛盾、价值观与行为、语言风格证据、动机推断边界和关系一致性。"
                "对虚构角色必须严格防范 OOC 魔改：严禁将同人私设、单一烂梗或性格反转"
                "（如高冷变热情、专业医生变无脑霸总）作为人物真实设定。"
                "每个 required dimension 必须给出 pass/repair_required/fail。最终门禁评估的是"
                "产物是否忠实于现有资料，不以编造内容换取表面完整：资料本身缺少第三方评价、"
                "生平或逐字样本时，若 artifact 已诚实保留缺口，应判 pass 并记录 coverage gap；"
                "只有错误归因、无来源断言、结构损坏或未处理的真实矛盾才需返工。"
            )
        return AgentTurn(
            user_message=json.dumps(
                {"audit_type": audit_type, "instruction": instruction, **payload},
                ensure_ascii=False,
                default=str,
            ),
            system_prompt=(
                "你是 Persona Continuum 严格最终质量门禁。只能依据输入 Ledger 与 artifacts；"
                "不得把推断或模拟延续当成历史事实。返回符合 schema 的单个 JSON 对象。"
                "Ledger 的 omitted_evidence_id_count 标明省略的引用数量，完整来源链按条目 id "
                "保存在本地；引用样本不代表全部证据，不要把省略的引用当成缺失来源。"
            ),
            expected_output=FINAL_AUDIT_SCHEMA,
        )

    async def _run_final_audit_pass(
        self,
        job: PersonaCreationJob,
        audit_type: str,
        payload: dict[str, Any],
        *,
        attempt: int,
    ) -> dict[str, Any]:
        turn = self._final_audit_turn(audit_type, payload)
        participant_id = f"final_{audit_type}_audit:{attempt}"
        try:
            result = await self._agent_json(
                job,
                user_message=turn.user_message,
                system_prompt=turn.system_prompt or "",
                participant_id=participant_id,
                phase=f"final_{audit_type}_audit",
                schema=FINAL_AUDIT_SCHEMA,
            )
        finally:
            await self._drop_job_session(job.id, participant_id)
        if not isinstance(result, dict):
            raise PersonaQualityGateError(
                f"final_{audit_type}_audit_invalid",
                failure_code=PersonaFailureCode.FINAL_AUDIT_FAILED.value,
            )
        return dict(result)

    @staticmethod
    def _audit_failing_dimensions(
        results: dict[str, dict[str, Any]], *, preserve_documented_source_gaps: bool = False
    ) -> set[str]:
        failing: set[str] = set()
        issue_types_by_dimension: dict[str, set[str]] = {}
        for result in results.values():
            dimensions = result.get("dimensions")
            if not isinstance(dimensions, dict):
                dimensions = {}
            # A dimension the auditor did not list inherits the overall
            # verdict: "pass" with an empty map is a pass, not eight failures,
            # while a non-pass overall status still fails every unlisted one.
            overall = result.get("status")
            for dimension in REQUIRED_DIMENSIONS:
                raw = dimensions.get(dimension, overall)
                # ``FINAL_AUDIT_SCHEMA`` keeps the per-dimension entry
                # free-form, so models return ``status``, ``verdict``,
                # ``result`` or a bare string.  Reading only ``status``
                # turned an all-pass audit into eight false failures.
                if isinstance(raw, dict):
                    status = next(
                        (
                            raw[key]
                            for key in ("status", "verdict", "result", "state")
                            if raw.get(key) is not None
                        ),
                        None,
                    )
                else:
                    status = raw
                if str(status or "").casefold() != "pass":
                    failing.add(dimension)
            for issue in result.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                # A finding may name one dimension, several, or none
                # (cross-cutting).  Never test an untrusted field directly for
                # set/list membership: that is what raised
                # "unhashable type: 'list'".
                issue_dimensions = [
                    candidate
                    for candidate in normalize_audit_issue_dimensions(issue)
                    if candidate in REQUIRED_DIMENSIONS
                ]
                if not issue_dimensions:
                    # Global findings are attributed to the whole artifact and
                    # only escalate through explicit severity below.
                    continue
                issue_type = str(issue.get("type") or "").casefold()
                severity = str(issue.get("severity") or "").casefold()
                for dimension in issue_dimensions:
                    issue_types_by_dimension.setdefault(dimension, set()).add(issue_type)
                    if severity in AUDIT_BLOCKING_SEVERITIES:
                        failing.add(dimension)
        if preserve_documented_source_gaps:
            source_gap_types = {
                "content_thinness",
                "documented_gap",
                "evidence_gap",
                "missing_core_sections",
                "missing_dimension_content",
                "no_verbatim_samples",
                "single_source_no_cross_validation",
            }
            for dimension, issue_types in issue_types_by_dimension.items():
                if issue_types and issue_types <= source_gap_types:
                    failing.discard(dimension)
        return failing

    async def _repair_audit_dimensions(
        self,
        job: PersonaCreationJob,
        dimensions: set[str],
        *,
        attempt: int,
        audit_results: dict[str, dict[str, Any]] | None = None,
        target_components_by_dimension: dict[str, list[str]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not job.persona_id or not job.compilation_task_id:
            raise PersonaQualityGateError(
                "audit_repair_binding_missing",
                failure_code=PersonaFailureCode.AUDIT_REPAIR_FAILED.value,
            )
        sources = list(self.continuum.personas.get_sources(job.persona_id))
        source_by_id = {source.id: source for source in sources}
        # Shared by every repair worker; evidence is read-only during repair.
        evidence_index = (
            self.material_intelligence.get_index(job.persona_id)
            if self.material_intelligence is not None
            else None
        )
        context_manager = getattr(
            self.runtime_executor, "context_budget_manager", AgentContextBudgetManager()
        )
        current_artifacts = self._latest_dimension_artifacts(job)
        pending_dimensions = [dim for dim in REQUIRED_DIMENSIONS if dim in dimensions]
        concurrency_limit = max(
            1,
            safe_int(
                getattr(self.continuum.config, "persona_dimension_concurrency", 3),
                default=3,
                minimum=1,
            )
            or 3,
        )
        semaphore = asyncio.Semaphore(concurrency_limit)

        async def repair_one(dimension: str) -> dict[str, Any]:
            # Each repair consumes immutable inputs (audit findings plus a
            # snapshot of the current artifact) and returns its own artifact;
            # the deterministic canonical-order merge happens after the gather.
            async with semaphore:
                # Workers still queued behind the semaphore must not start a
                # repair model call once a pause has been requested.
                self._raise_if_pause_requested(job.id)
                target_components = list(
                    (target_components_by_dimension or {}).get(dimension, [])
                )
                current_artifact = current_artifacts.get(dimension, {})

                async def retrieved_evidence_items() -> list[dict[str, Any]]:
                    retrieved = (
                        await asyncio.to_thread(
                            evidence_index.retrieve, dimension, top_k=32, diversity=True
                        )
                        if evidence_index is not None
                        else []
                    )
                    return [
                        {
                            "evidence_id": item.get("id"),
                            "source_ids": [
                                str(value)
                                for value in item.get("source_ids") or []
                                if str(value) in source_by_id
                            ],
                            "evidence_type": item.get("kind"),
                            "verbatim_samples": item.get("verbatim_samples") or [],
                            "intelligence": item.get("intelligence") or {},
                            "content": item.get("text") or "",
                        }
                        for item in retrieved
                    ]

                if target_components:
                    evidence_items = self._targeted_repair_evidence_items(
                        current_artifact,
                        known_source_ids=set(source_by_id),
                    )
                    if not evidence_items:
                        # An empty or claim-less artifact has no claim slice
                        # to repair from; without the index fallback the
                        # targeted repair could never rebuild anything.
                        evidence_items = await retrieved_evidence_items()
                else:
                    evidence_items = await retrieved_evidence_items()
                if not evidence_items:
                    if target_components:
                        return {
                            "dimension": dimension,
                            "artifact": None,
                            "outcomes": {
                                component: "gap_no_evidence"
                                for component in target_components
                            },
                        }
                    evidence_items = [
                        {
                            "evidence_id": source.id,
                            "source_ids": [source.id],
                            "evidence_type": "source",
                            "verbatim_samples": [],
                            "content": source.content,
                        }
                        for source in sources
                    ]
                participant_id = f"{dimension}:repair:{attempt}"
                # A cross-dimension finding reaches every dimension it names;
                # ``_global`` / ``cross_cutting`` findings are broadcast.  The
                # model may send ``dimension`` as a string, a list, or omit it,
                # so every shape is normalized before matching.
                dimension_findings = [
                    normalize_audit_issue(issue)
                    for result in (audit_results or {}).values()
                    for issue in issues_for_dimension(result.get("issues"), dimension)
                ]
                artifact_context = (
                    self._targeted_repair_artifact_slice(
                        current_artifact,
                        target_components,
                    )
                    if target_components
                    else self._compact_audit_artifact(current_artifact, claims_limit=16)
                )
                repair_context = {
                    "instruction": (
                        "仅修复审计指出的产物缺陷；保留当前 artifact 中仍有来源支持的内容。"
                        "不得把资料缺失当作需要补写的内容，也不得改变已正确的 claim 类型。"
                    ),
                    "audit_findings": dimension_findings[:12],
                    "target_components": target_components,
                    "evidence_gate": (
                        "只能为 target_components 输出同名 canonical component；"
                        "每项内容必须能由本次 EVIDENCE 或 current_artifact_slice 直接支持。"
                        "证据不足时保持空值，并在 uncertainty.notes 写 KEEP GAP。"
                    )
                    if target_components
                    else None,
                    "current_artifact_slice": artifact_context,
                }
                try:
                    repaired = await self._run_dimension_extraction(
                        job,
                        dimension=dimension,
                        evidence_items=evidence_items,
                        participant_id=participant_id,
                        mode="targeted_repair",
                        source_by_id=source_by_id,
                        context_manager=context_manager,
                        phase="targeted_repair",
                        repair_context=repair_context,
                        # Content the repair keeps from the current artifact
                        # was already validated against these sources.
                        carried_source_ids={
                            str(value) for value in current_artifact.get("source_ids") or []
                        },
                    )
                finally:
                    await self._drop_job_session(job.id, participant_id)
                if not target_components:
                    return {"dimension": dimension, "artifact": repaired, "outcomes": {}}
                merged, outcomes = self._merge_targeted_repair_artifact(
                    current_artifact,
                    repaired.model_dump(mode="json"),
                    target_components=target_components,
                    allowed_source_ids={
                        str(source_id)
                        for item in evidence_items
                        for source_id in item.get("source_ids") or []
                    },
                )
                return {"dimension": dimension, "artifact": merged, "outcomes": outcomes}

        outcomes = await asyncio.gather(
            *(repair_one(dimension) for dimension in pending_dimensions),
            return_exceptions=True,
        )
        by_dimension: dict[str, Any] = {}
        repair_outcomes: dict[str, dict[str, Any]] = {}
        failures: dict[str, BaseException] = {}
        for dimension, outcome in zip(pending_dimensions, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, _PauseRequested):
                    # A pause is control-plane state, not an audit failure:
                    # surface it so the worker enters PAUSED instead of being
                    # marked failed_quality_gate.
                    raise outcome
                failures[dimension] = outcome
                continue
            repaired_result: dict[str, Any] = outcome
            by_dimension[repaired_result["dimension"]] = repaired_result["artifact"]
            repair_outcomes[repaired_result["dimension"]] = dict(
                repaired_result.get("outcomes") or {}
            )
        compilation_service = self.continuum.compilation
        # Successful siblings are persisted even when another dimension's
        # repair failed, so their model work is never thrown away.
        for dimension in pending_dimensions:
            repaired = by_dimension.get(dimension)
            if repaired is None:
                continue
            artifact_payload = (
                repaired.model_dump(mode="json")
                if hasattr(repaired, "model_dump")
                else dict(repaired)
            )
            try:
                # Pydantic may normalize legacy memory types while validating
                # the full settled artifact. Hash the validated payload,
                # exactly as submit_research_artifact will, or the safety check
                # correctly rejects the repair as an artifact_hash_mismatch.
                artifact_payload = ResearchArtifact.model_validate(
                    artifact_payload
                ).model_dump(mode="json")
                artifact_payload["artifact_hash"] = (
                    compilation_service._canonical_artifact_sha256(artifact_payload)
                )
                compilation_service.submit_research_artifact(
                    job.compilation_task_id, artifact_payload
                )
            except (ValueError, CodedError, PersonaCreationError) as exc:
                failures[dimension] = exc
                continue
            job.dimension_progress[dimension] = max(
                safe_int(job.dimension_progress.get(dimension, 0), default=0, minimum=0) or 0,
                len(artifact_payload.get("source_ids") or []),
                1,
            )
            await self._emit(
                job,
                "persona_audit_dimension_repaired",
                dimension=dimension,
                attempt=attempt,
            )
        if failures:
            recorded = job.job_config.setdefault("audit_repair_failures", {})
            for dimension, failure in failures.items():
                recorded[dimension] = sanitize_diagnostic(failure)
                repair_outcomes.setdefault(dimension, {})["__repair__"] = "failed"
                await self._emit(
                    job,
                    "persona_audit_dimension_repair_failed",
                    dimension=dimension,
                    attempt=attempt,
                    error=sanitize_diagnostic(failure)[:300],
                )
        self._save(job)
        runtime_failure = next(
            (
                exc
                for exc in failures.values()
                if not self._repair_failure_is_quality_issue(exc)
            ),
            None,
        )
        if runtime_failure is not None:
            # Transport, quota and timeout failures are not verdicts on the
            # artifact; they keep their own retry/pause semantics.
            raise runtime_failure
        # A quality failure (unattributable output, invalid artifact) keeps the
        # current artifact; the next audit pass decides whether it still fails.
        return repair_outcomes

    @staticmethod
    def _repair_failure_is_quality_issue(exc: BaseException) -> bool:
        """Whether a repair failure judges the artifact rather than the runtime."""

        if isinstance(exc, AgentOutputError | StructuredOutputSchemaError):
            return True
        if isinstance(exc, AgentRuntimeError):
            return False
        return isinstance(exc, PersonaCreationError | ValueError | CodedError)

    @staticmethod
    def _targeted_repair_evidence_items(
        artifact: dict[str, Any], *, known_source_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Build a fail-closed evidence slice from character-visible claims."""

        items: list[dict[str, Any]] = []
        for index, claim in enumerate(artifact.get("claims") or []):
            if not isinstance(claim, dict):
                continue
            raw_metadata = claim.get("metadata")
            metadata: dict[str, Any] = (
                raw_metadata if isinstance(raw_metadata, dict) else {}
            )
            if normalise_material_scope(metadata.get("material_scope")) != CHARACTER_VISIBLE:
                continue
            source_id = str(claim.get("source_id") or "")
            if not source_id or source_id not in known_source_ids:
                continue
            content = str(claim.get("content") or "").strip()
            if not content:
                continue
            items.append(
                {
                    "evidence_id": f"claim_slice_{index}",
                    "source_ids": [source_id],
                    "evidence_type": "character_visible_claim",
                    "verbatim_samples": [],
                    "intelligence": {"material_scope": CHARACTER_VISIBLE},
                    "content": content,
                }
            )
        return items[:16]

    @staticmethod
    def _targeted_repair_artifact_slice(
        artifact: dict[str, Any], target_components: list[str]
    ) -> dict[str, Any]:
        def character_visible(claim: Any) -> bool:
            if not isinstance(claim, dict):
                return False
            raw_metadata = claim.get("metadata")
            if not isinstance(raw_metadata, dict):
                return False
            return (
                normalise_material_scope(raw_metadata.get("material_scope"))
                == CHARACTER_VISIBLE
            )

        claim_refs: list[str] = []
        for claim in artifact.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            raw_metadata = claim.get("metadata")
            metadata: dict[str, Any] = (
                raw_metadata if isinstance(raw_metadata, dict) else {}
            )
            if character_visible(claim):
                claim_refs.extend(str(value) for value in metadata.get("evidence_ids") or [])
        return {
            "dimension": artifact.get("dimension"),
            "target_components": list(target_components),
            "character_visible_claim_count": sum(
                1
                for cls in artifact.get("claims") or []
                if character_visible(cls)
            ),
            "evidence_refs": sorted(set(claim_refs))[:32],
            "extracted_components": {},
            "conflicts": [],
            "uncertainty": {
                "level": 0.5,
                "notes": ["bounded character-visible targeted repair slice"],
            },
        }

    @staticmethod
    def _merge_targeted_repair_artifact(
        current: dict[str, Any],
        repaired: dict[str, Any],
        *,
        target_components: list[str],
        allowed_source_ids: set[str],
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Keep settled fields and accept only evidence-gated target slots."""

        merged = json.loads(json.dumps(current, ensure_ascii=False, default=str))
        merged_components = dict(merged.get("extracted_components") or {})
        repaired_components = dict(repaired.get("extracted_components") or {})
        outcomes: dict[str, str] = {}
        accepted = False
        for component in target_components:
            value = repaired_components.get(component)
            if value in (None, "", [], {}) or not allowed_source_ids:
                outcomes[component] = "gap_insufficient_evidence"
                continue
            merged_components[component] = value
            outcomes[component] = "repaired"
            accepted = True
        if not accepted:
            return None, outcomes

        merged["extracted_components"] = merged_components
        existing_claim_keys = {
            (str(item.get("content") or ""), str(item.get("source_id") or ""))
            for item in merged.get("claims") or []
            if isinstance(item, dict)
        }
        for claim in repaired.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            source_id = str(claim.get("source_id") or "")
            content = str(claim.get("content") or "").strip()
            if not content or source_id not in allowed_source_ids:
                continue
            metadata = dict(claim.get("metadata") or {})
            metadata["material_scope"] = CHARACTER_VISIBLE
            claim["metadata"] = metadata
            key = (content, source_id)
            if key not in existing_claim_keys:
                merged.setdefault("claims", []).append(claim)
                existing_claim_keys.add(key)
                if source_id and source_id not in merged.get("source_ids", []):
                    merged.setdefault("source_ids", []).append(source_id)

        # Handle any memories attached during targeted repair as well
        existing_memory_keys = {
            (str(item.get("content") or ""), str(item.get("source_id") or ""))
            for item in merged.get("memories") or []
            if isinstance(item, dict)
        }
        for memory in repaired.get("memories") or []:
            if not isinstance(memory, dict):
                continue
            source_id = str(memory.get("source_id") or "")
            content = str(memory.get("content") or "").strip()
            if not content or source_id not in allowed_source_ids:
                continue
            key = (content, source_id)
            if key not in existing_memory_keys:
                merged.setdefault("memories", []).append(memory)
                existing_memory_keys.add(key)
                if source_id and source_id not in merged.get("source_ids", []):
                    merged.setdefault("source_ids", []).append(source_id)

        # Guarantee: ensure all referenced source_ids in claims/memories are in source_ids
        referenced_source_ids = {
            str(c.get("source_id"))
            for c in (merged.get("claims") or []) + (merged.get("memories") or [])
            if isinstance(c, dict) and c.get("source_id")
        }
        current_source_ids = set(merged.get("source_ids") or [])
        merged["source_ids"] = sorted(current_source_ids | referenced_source_ids)

        merged["artifact_id"] = new_id("art")
        merged["artifact_hash"] = hashlib.sha256(
            json.dumps(merged, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        merged.pop("artifact_canonical_sha256", None)
        return merged, outcomes

    def _finalize_targeted_repair_job(
        self,
        job: PersonaCreationJob,
        task: Any,
        coverage: dict[str, Any],
    ) -> None:
        """Synchronize production state after bounded repair and compilation."""

        complete = bool(task.status == "completed" and coverage.get("ok"))
        job.status = "completed" if complete else "completed_with_gaps"
        job.current_stage = job.status
        job.error = None
        job.failure_json = None
        job.progress.failure = None
        job.progress.update_stage(
            job.current_stage,
            label="Persona 创建完成" if complete else "完成（存在资料缺口）",
            percent=100,
            completed=job.source_count,
            total=job.research_policy.preferred_source_target,
        )
        job.touch_worker(WorkerState.FINISHED, finished_at=datetime.now(UTC).isoformat())
        if not job.persona_id:
            return
        manifest = self.continuum.personas.get(job.persona_id).manifest
        manifest.compile_state = "compiled" if complete else "completed_with_gaps"
        manifest.active = complete
        record = self.continuum.personas.update_manifest(manifest)
        if self.profile_library is not None:
            self.profile_library.sync_persona(record)

