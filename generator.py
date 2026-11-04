from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from astrbot import logger

from .model import build_system_prompt, normalize_persona
from .prompts import (
    AUDIT_PROMPT,
    FACT_PROMPT,
    PERSONA_PROMPT,
    REPAIR_PROMPT,
    audit_user_prompt,
    facts_user_prompt,
    persona_user_prompt,
)

Progress = Callable[[str, str, int], None]


class PersonaGenerationError(RuntimeError):
    pass


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def extract_json(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    candidate = _strip_fences(text)
    for attempt in (candidate, _remove_trailing_commas(candidate)):
        try:
            value = json.loads(attempt)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
    start = candidate.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    block = candidate[start : index + 1]
                    for variant in (block, _remove_trailing_commas(block)):
                        try:
                            value = json.loads(variant)
                        except ValueError:
                            continue
                        if isinstance(value, dict):
                            return value
                    break
        start = candidate.find("{", start + 1)
    return None


def _fit_facts_json(facts: list[dict[str, Any]], limit: int = 18000) -> str:
    selected = facts
    while selected:
        text = json.dumps({"facts": selected}, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= limit:
            return text
        selected = selected[: max(1, len(selected) // 2)]
    return '{"facts":[]}'


class PersonaGenerator:
    def __init__(
        self,
        context,
        *,
        provider_id: str,
        depth: str,
        progress: Progress,
    ) -> None:
        self.context = context
        self.provider_id = provider_id
        self.depth = depth if depth in {"quick", "standard", "deep"} else "standard"
        self.progress = progress
        self.model = ""

    def _set_model(self) -> None:
        provider = self.context.get_provider_by_id(self.provider_id)
        if provider is None:
            return
        try:
            self.model = str(provider.meta().model)
        except Exception:
            self.model = ""

    async def _call_text(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=self.provider_id,
                    system_prompt=system_prompt,
                    prompt=user_prompt,
                ),
                timeout=300,
            )
        except asyncio.TimeoutError as exc:
            raise PersonaGenerationError("模型调用超时，请重试或换一个模型。") from exc
        except Exception as exc:
            raise PersonaGenerationError(f"模型调用失败：{exc}") from exc
        text = getattr(response, "completion_text", None)
        if not isinstance(text, str) or not text.strip():
            raise PersonaGenerationError("模型没有返回文字结果，请换一个模型重试。")
        return text

    async def _call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        stage: str,
        percent: int,
    ) -> dict[str, Any]:
        self.progress(stage, "正在调用模型…", percent)
        raw = await self._call_text(system_prompt, user_prompt)
        data = extract_json(raw)
        if data is not None:
            return data
        logger.warning("[persona_workshop] 模型输出不是合法 JSON，尝试修复：%s", raw[:300])
        repaired = await self._call_text(
            REPAIR_PROMPT,
            "请把下面的内容修复为合法 JSON，只输出 JSON：\n\n" + raw[:16000],
        )
        data = extract_json(repaired)
        if data is None:
            excerpt = raw.strip().replace("\n", " ")[:500]
            raise PersonaGenerationError(
                "模型输出无法解析为 JSON。可以换一个模型或减少资料后重试。"
                f"原始输出片段：{excerpt}"
            )
        return data

    async def generate(
        self,
        *,
        name: str,
        source_type: str,
        material: str,
        notes: str,
    ) -> dict[str, Any]:
        self._set_model()
        self.progress("facts", "正在提取资料中的事实…", 15)
        facts_data = await self._call_json(
            FACT_PROMPT,
            facts_user_prompt(
                name=name,
                source_type=source_type,
                material=material,
                notes=notes,
            ),
            stage="facts",
            percent=25,
        )
        facts = facts_data.get("facts")
        if not isinstance(facts, list):
            facts = []
        facts = [item for item in facts if isinstance(item, dict)][:200]

        self.progress("persona", "正在编译人格结构…", 45)
        persona_data = await self._call_json(
            PERSONA_PROMPT,
            persona_user_prompt(
                name=name,
                source_type=source_type,
                material=material,
                notes=notes,
                facts_json=_fit_facts_json(facts),
            ),
            stage="persona",
            percent=60,
        )
        persona = normalize_persona(persona_data, fallback_name=name)
        issues: list[str] = []

        if self.depth in {"standard", "deep"}:
            self.progress("audit", "正在审阅人格一致性…", 75)
            try:
                audit_data = await self._call_json(
                    AUDIT_PROMPT,
                    audit_user_prompt(
                        persona_json=json.dumps(persona, ensure_ascii=False, indent=2),
                    ),
                    stage="audit",
                    percent=85,
                )
            except PersonaGenerationError as exc:
                logger.warning("[persona_workshop] 审阅阶段失败，保留初稿：%s", exc)
                issues.append(f"审阅未完成，当前为初稿：{exc}")
            else:
                corrected = audit_data.get("persona")
                if isinstance(corrected, dict):
                    persona = normalize_persona(corrected, fallback_name=name)
                raw_issues = audit_data.get("issues")
                if isinstance(raw_issues, list):
                    issues = [str(item).strip() for item in raw_issues if str(item).strip()][:20]

        persona["name"] = name
        persona["source_type"] = source_type
        persona["facts"] = facts
        persona["provider_id"] = self.provider_id
        persona["model"] = self.model
        persona["system_prompt"] = build_system_prompt(persona)
        self.progress("done", "人格已生成", 100)
        return {
            "persona": persona,
            "facts": facts,
            "issues": issues,
            "provider_id": self.provider_id,
            "model": self.model,
        }
