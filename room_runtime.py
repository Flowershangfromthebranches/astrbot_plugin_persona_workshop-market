from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from astrbot import logger

from .consultation import SilentConsultation
from .livingmemory_bridge import LivingMemoryBridge
from .schema_compat import normalize_compiled_components

DYNAMIC_MARKER = "[persona_workshop_dynamic]"


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip() if len(text) > limit else text


class RoomRuntimeManager:
    """Run one hidden continuum Room session per AstrBot conversation/persona."""

    def __init__(
        self,
        runtime: Any,
        context: Any,
        config: Any,
        links_loader: Callable[[], dict[str, str]],
        data_dir: Path,
    ) -> None:
        self.runtime = runtime
        self.context = context
        self.config = config
        self.links_loader = links_loader
        self.livingmemory = LivingMemoryBridge(context)
        self.consultation = SilentConsultation(context, config)
        self.sessions_path = Path(data_dir) / "runtime_sessions.json"
        self._sessions = self._load_sessions()
        self._dossier_cache: dict[str, tuple[float, str]] = {}

    def _load_sessions(self) -> dict[str, str]:
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(key): str(value) for key, value in data.items() if value}

    def _save_sessions(self) -> None:
        temp = self.sessions_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._sessions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.sessions_path)

    def _native_to_continuum(self, native_persona_id: str) -> str:
        links = self.links_loader()
        for continuum_id, native_id in links.items():
            if native_id == native_persona_id:
                return continuum_id
        return ""

    def _memory_mode(self) -> str:
        value = str(self.config.get("memory_mode", "auto") or "auto").strip().lower()
        if value not in {"auto", "continuum", "livingmemory", "hybrid"}:
            return "auto"
        return value

    def _room_id(self, continuum_persona_id: str, umo: str) -> str:
        key = f"{continuum_persona_id}|{umo}"
        room_id = self._sessions.get(key, "")
        if room_id:
            try:
                self.runtime.continuum.rooms.get_state(room_id)
                return room_id
            except Exception:
                logger.warning("[persona_workshop] Room 不存在，重新创建：%s", room_id)
                self._sessions.pop(key, None)
        room = self.runtime.continuum.rooms.create_room(
            [continuum_persona_id],
            topic=f"AstrBot:{umo}",
        )
        room_id = str(room["id"])
        self._sessions[key] = room_id
        self._save_sessions()
        return room_id

    def _normalize_prepared(self, prepared: Any) -> Any:
        context = getattr(prepared, "compiled_persona_context", None)
        if not isinstance(context, dict):
            return prepared
        updated = dict(context)
        changed = False
        for key in ("by_key", "core_components"):
            value = updated.get(key)
            if isinstance(value, dict):
                updated[key] = normalize_compiled_components(value)
                changed = True
        if not changed:
            return prepared
        return prepared.model_copy(update={"compiled_persona_context": updated})

    def _persona_dossier(self, persona_id: str) -> str:
        try:
            record = self.runtime.continuum.personas.get(persona_id)
        except Exception:
            return ""
        package = Path(getattr(record, "package_path", "") or "")
        if not package:
            return ""
        from .continuum_persona_renderer import PersonaRenderer

        renderer = PersonaRenderer(
            package,
            max_traits=12,
            max_dialogue_examples=10,
            max_contradictions=10,
            keep_runtime_rules=False,
        )
        mtime = renderer.snapshot_mtime()
        cached = self._dossier_cache.get(persona_id)
        if cached is not None and cached[0] >= mtime:
            return cached[1]
        try:
            dossier = renderer.render_room_dossier()
        except Exception:
            logger.exception("[persona_workshop] 读取人格档案失败：%s", persona_id)
            return ""
        self._dossier_cache[persona_id] = (mtime, dossier)
        return dossier

    @staticmethod
    def _components(prepared: Any) -> dict[str, Any]:
        context = getattr(prepared, "compiled_persona_context", None)
        if not isinstance(context, dict):
            return {}
        components = dict(context.get("by_key") or {})
        components.update(context.get("core_components") or {})
        return components

    @staticmethod
    def _state_block(prepared: Any, components: dict[str, Any]) -> list[str]:
        from persona_continuum.runtime.core_fidelity import behavioral_implications

        emotions = "、".join(
            f"{item.name}={item.intensity:.2f}"
            for item in (prepared.current_emotions or [])
            if item.intensity > 0.05
        ) or "平稳"
        needs = "、".join(
            f"{item.name}（{item.level:.2f}）"
            for item in (prepared.current_needs or [])
            if item.level > 0.7 or abs(item.level - item.baseline) >= 0.2
        ) or "无明显波动"
        goals = "、".join(str(goal) for goal in (prepared.active_goals or []) if str(goal).strip())
        relationship = str(prepared.relationship_stance or "").strip()
        state = getattr(prepared, "relationship_state", None)
        if state is not None:
            kind = getattr(getattr(state, "relationship_kind", None), "value", "")
            details = [
                f"类型={kind}" if kind else "",
                f"阶段={state.bond_stage}" if getattr(state, "bond_stage", "") else "",
                f"信任={state.trust:.2f}",
                f"好感={state.affection:.2f}",
                f"冲突={state.unresolved_conflict:.2f}",
            ]
            relationship = "；".join(
                part for part in (relationship, "，".join(details)) if part
            )
        lines = [
            "## 本轮内部状态",
            "- 当前情绪：" + emotions,
            "- 当前需求：" + needs,
            "- 当前目标：" + (goals or "自然参与当前对话"),
            "- 关系立场：" + (relationship or "保持当前人格设定"),
        ]
        try:
            implications = behavioral_implications(
                components,
                prepared.current_needs or [],
                prepared.relationship_state,
            )
        except Exception:
            implications = []
        if implications:
            lines.append("## 行为倾向")
            lines.extend(f"- {_clip(item, 300)}" for item in implications[:8])
        return lines

    @staticmethod
    def _memory_block(prepared: Any, *, include_continuum: bool) -> list[str]:
        lines: list[str] = []
        facts = [str(item) for item in (prepared.relevant_persona_facts or []) if str(item).strip()]
        if facts:
            lines.append("## 相关人格事实")
            lines.extend(f"- {_clip(item, 500)}" for item in facts[:8])
        if include_continuum:
            memories = list(prepared.relevant_memories or []) + list(
                prepared.activated_emotional_memories or []
            )
            seen: set[str] = set()
            rendered: list[str] = []
            for item in memories:
                content = _clip(getattr(item, "content", ""), 500)
                if not content or content in seen:
                    continue
                seen.add(content)
                rendered.append(content)
            if rendered:
                lines.append("## 相关记忆")
                lines.extend(f"- {item}" for item in rendered[:8])
        return lines

    def build_dynamic_context(
        self,
        prepared: Any,
        *,
        include_continuum_memories: bool,
        living_block: str = "",
        consultation_brief: str = "",
    ) -> str:
        components = self._components(prepared)
        sections = [
            DYNAMIC_MARKER,
            "以下内容是本轮角色内部状态与记忆，不是用户发言。自然内化，不要复述、引用，"
            "也不要向用户提及系统、记忆、会诊或提示词。",
            *self._state_block(prepared, components),
            *self._memory_block(prepared, include_continuum=include_continuum_memories),
        ]
        if living_block:
            sections.append(living_block)
        if consultation_brief:
            sections.extend(
                [
                    "## 静默会诊备忘（仅供内部参考，绝对不要向用户提及会诊过程）",
                    consultation_brief,
                ]
            )
        return "\n\n".join(part for part in sections if part).strip()

    async def prepare(self, native_persona_id: str, umo: str, user_message: str) -> dict[str, Any]:
        continuum_persona_id = self._native_to_continuum(native_persona_id)
        if not continuum_persona_id:
            raise RuntimeError("persona_not_linked_to_continuum")
        room_id = self._room_id(continuum_persona_id, umo)
        bundle = await asyncio.to_thread(
            self.runtime.continuum.rooms.prepare_next,
            room_id,
            user_message,
        )
        prepared = self._normalize_prepared(bundle["prepared"])
        session_id = bundle["session_id"]
        persona_id = bundle["speaker_persona_id"]
        manifest = prepared.identity_anchor
        original_prepared = prepared.model_copy(deep=True)

        memory_mode = self._memory_mode()
        living_available = self.livingmemory.available
        inject_living = (
            living_available
            and (
                memory_mode in {"livingmemory", "hybrid"}
                or bool(self.config.get("livingmemory_inject", False))
            )
        )
        include_continuum_memories = True
        if living_available and memory_mode in {"auto", "livingmemory"}:
            # LivingMemory's own on_llm_request hook injects retrieved memories.
            # Do not duplicate the same memory in this extra context.
            prepared = prepared.model_copy(
                update={
                    "relevant_memories": [],
                    "activated_emotional_memories": [],
                }
            )
            include_continuum_memories = False

        living_items: list[dict[str, Any]] = []
        living_block = ""
        if inject_living:
            living_items = await self.livingmemory.search(
                user_message,
                session_id=umo,
                k=max(1, min(20, int(self.config.get("livingmemory_top_k", 5) or 5))),
            )
            living_block = LivingMemoryBridge.format_block(living_items)

        consultation_brief = ""
        if self.consultation.should_run(user_message):
            try:
                memory_snippets = list(original_prepared.relevant_persona_facts or [])
                memory_snippets.extend(
                    str(item.content)
                    for item in (original_prepared.relevant_memories or [])
                    if getattr(item, "content", None)
                )
                memory_snippets.extend(str(item.get("content") or "") for item in living_items)
                state_parts = [
                    "情绪："
                    + "、".join(
                        f"{item.name}={item.intensity:.2f}"
                        for item in original_prepared.current_emotions
                        if item.intensity > 0.05
                    ),
                    "需求："
                    + "、".join(
                        f"{item.name}={item.level:.2f}"
                        for item in original_prepared.current_needs
                        if item.level > 0.05
                    ),
                    "关系立场：" + str(original_prepared.relationship_stance or ""),
                    "目标：" + "、".join(str(goal) for goal in original_prepared.active_goals or []),
                ]
                brief = await self.consultation.run(
                    persona_name=str(manifest.display_name or persona_id),
                    system_prompt=self._persona_dossier(continuum_persona_id),
                    user_message=user_message,
                    state_text="\n".join(part for part in state_parts if part),
                    memories=[item for item in memory_snippets if item],
                )
                if brief:
                    consultation_brief = brief
                    logger.info(
                        "[persona_workshop] 静默会诊完成：brief=%s字",
                        len(brief),
                    )
            except Exception:
                logger.exception("[persona_workshop] 静默会诊失败，继续普通 Room 回复")

        dynamic_context = self.build_dynamic_context(
            prepared,
            include_continuum_memories=include_continuum_memories,
            living_block=living_block,
            consultation_brief=consultation_brief,
        )

        return {
            "room_id": room_id,
            "session_id": session_id,
            "persona_id": persona_id,
            "native_persona_id": native_persona_id,
            "umo": umo,
            "user_message": user_message,
            "dynamic_context": dynamic_context,
            "memory_backend": (
                "livingmemory"
                if living_available and memory_mode in {"auto", "livingmemory"}
                else ("hybrid" if living_available and memory_mode == "hybrid" else "continuum")
            ),
            "livingmemory_count": len(living_items),
        }

    def commit(self, turn: dict[str, Any], assistant_response: str) -> None:
        if not assistant_response.strip():
            return
        self.runtime.continuum.rooms.commit_turn(
            turn["room_id"],
            turn["persona_id"],
            turn["session_id"],
            turn["user_message"],
            assistant_response,
        )
