from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import request
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig

from .generator import PersonaGenerationError, PersonaGenerator
from .model import MAX_PROMPT_CHARS, build_system_prompt, normalize_persona, persona_summary
from .store import PersonaStore

NAME = "astrbot_plugin_persona_workshop"
DEFAULT_NATIVE_PREFIX = "人格工坊·"
SOURCE_TYPES = {"fictional", "original", "real"}
DEPTHS = {"quick", "standard"}
JOB_TTL_SECONDS = 2 * 60 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _error(message: str, data: Any = None) -> dict[str, Any]:
    result = {"status": "error", "message": message}
    if data is not None:
        result["data"] = data
    return result


def _clip(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class PersonaWorkshop(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.conf = config or {}
        self.store = PersonaStore(Path(StarTools.get_data_dir(NAME)))
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: set[asyncio.Task] = set()
        self._job_lock = asyncio.Lock()
        self._continuum = None
        self._continuum_lock = asyncio.Lock()
        self._room_runtime = None
        self._room_runtime_lock = asyncio.Lock()
        self._pending_room_turns: dict[Any, dict[str, Any]] = {}
        for route, handler, methods in (
            ("state", self.state, ["GET"]),
            ("providers", self.providers, ["GET"]),
            ("generate/start", self.start_generation, ["POST"]),
            ("generate/status", self.generation_status, ["GET"]),
            ("persona", self.persona_detail, ["GET"]),
            ("save", self.save_persona, ["POST"]),
            ("compile", self.compile_prompt, ["POST"]),
            ("use", self.use_persona, ["POST"]),
            ("delete", self.delete_persona, ["POST"]),
            ("continuum/state", self.continuum_state, ["GET"]),
            ("continuum/job/create", self.continuum_create_job, ["POST"]),
            ("continuum/job/status", self.continuum_job_status, ["GET"]),
            ("continuum/job/action", self.continuum_job_action, ["POST"]),
            ("continuum/personas", self.continuum_personas, ["GET"]),
            ("continuum/runtime/status", self.continuum_runtime_status, ["GET"]),
            ("continuum/persona/use", self.continuum_use_persona, ["POST"]),
            ("continuum/persona/delete", self.continuum_delete_persona, ["POST"]),
            ("continuum/research/validate", self.continuum_validate_research, ["POST"]),
            ("continuum/material/upload", self.continuum_upload_material, ["POST"]),
            ("native/use", self.use_native_persona, ["POST"]),
        ):
            context.register_web_api(
                f"/{NAME}/page/{route}",
                handler,
                methods,
                f"persona workshop {route}",
            )

    def _max_source_chars(self) -> int:
        try:
            return max(1000, int(self.conf.get("max_source_chars", 30000)))
        except (TypeError, ValueError):
            return 30000

    def _bool(self, key: str, default: bool) -> bool:
        value = self.conf.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "off", "no", ""}
        return bool(value)

    def _native_prefix(self) -> str:
        value = str(self.conf.get("native_prefix", DEFAULT_NATIVE_PREFIX)).strip()
        return value or DEFAULT_NATIVE_PREFIX

    def _providers(self) -> list[dict[str, str]]:
        providers = []
        seen: set[str] = set()
        for provider in self.context.get_all_providers():
            try:
                meta = provider.meta()
            except Exception:
                continue
            provider_id = str(getattr(meta, "id", "") or "").strip()
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            providers.append(
                {
                    "id": provider_id,
                    "model": str(getattr(meta, "model", "") or ""),
                    "type": str(getattr(meta, "type", "") or ""),
                }
            )
        return providers

    def _config_provider_id(self) -> str:
        conf = self.context.get_config()
        runner = conf.get("agent_runner") or {}
        cfg = runner.get("config") or {}
        model = cfg.get("model") or {}
        return str(model.get("provider_id") or "").strip()

    async def _default_provider_id(self) -> str:
        configured = self._config_provider_id()
        providers = self._providers()
        ids = {item["id"] for item in providers}
        if configured and configured in ids:
            return configured
        try:
            provider = await self.context.get_using_provider_async()
            if provider is not None:
                provider_id = str(provider.meta().id)
                if provider_id in ids:
                    return provider_id
        except Exception:
            pass
        return providers[0]["id"] if providers else ""

    def _active_persona_id(self) -> str:
        conf = self.context.get_config()
        runner = conf.get("agent_runner") or {}
        cfg = runner.get("config") or {}
        if runner.get("runner_type", "local") == "local":
            persona = cfg.get("persona") or {}
            return str(persona.get("persona_id") or "").strip()
        return str(cfg.get("persona_id") or "").strip()

    def _set_active_persona(self, native_id: str) -> None:
        conf = self.context.get_config()
        runner = conf.get("agent_runner") or {}
        if not isinstance(runner, dict):
            runner = {}
        if runner.get("runner_type", "local") == "local":
            cfg = runner.setdefault("config", {})
            persona = cfg.setdefault("persona", {})
            persona["persona_id"] = native_id
        else:
            cfg = runner.setdefault("config", {})
            cfg["persona_id"] = native_id
        conf["agent_runner"] = runner
        conf.save_config()

    def _native_exists(self, native_id: str) -> bool:
        try:
            return self.context.persona_manager.get_persona_v3_by_id(native_id) is not None
        except Exception:
            return False

    def _new_native_id(self, name: str, persona_id: str) -> str:
        prefix = self._native_prefix()
        base = re.sub(r"[\x00-\x1f\x7f]", "", f"{prefix}{name}").strip()[:60] or f"{prefix}未命名"
        used = {
            str(item.get("native_persona_id") or "")
            for item in self.store.list()
            if str(item.get("id") or "") != persona_id
        }
        candidate = base
        index = 2
        while candidate in used or self._native_exists(candidate):
            candidate = f"{base}（{index}）"[:60]
            index += 1
            if index > 100:
                candidate = f"{prefix}{uuid.uuid4().hex[:6]}"
                break
        return candidate

    async def _sync_native(self, persona: dict[str, Any], system_prompt: str) -> str:
        native_id = str(persona.get("native_persona_id") or "").strip()
        if not native_id:
            native_id = self._new_native_id(persona.get("name", ""), persona.get("id", ""))
        if self._native_exists(native_id):
            await self.context.persona_manager.update_persona(
                native_id,
                system_prompt=system_prompt,
            )
        else:
            await self.context.persona_manager.create_persona(
                native_id,
                system_prompt,
                tools=None,
                skills=None,
            )
        return native_id

    def _persona_detail(self, persona: dict[str, Any]) -> dict[str, Any]:
        data = dict(persona)
        data["source_excerpt"] = self.store.source_excerpt(str(persona.get("id") or ""))
        return data

    def _cleanup_jobs(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        for job_id in [
            job_id
            for job_id, job in self.jobs.items()
            if job.get("status") in {"done", "error"}
            and float(job.get("created_ts") or 0) < cutoff
        ]:
            self.jobs.pop(job_id, None)

    async def state(self):
        active = self._active_persona_id()
        personas = []
        for persona in self.store.list():
            item = persona_summary(persona)
            item["active"] = item["native_persona_id"] == active
            personas.append(item)
        return _ok(
            {
                "personas": personas,
                "active_persona_id": active,
                "providers": self._providers(),
                "default_provider_id": await self._default_provider_id(),
                "max_source_chars": self._max_source_chars(),
                "native_prefix": self._native_prefix(),
                "depths": [
                    {"id": "quick", "label": "快速", "description": "2 次模型调用，适合资料完整的角色"},
                    {"id": "standard", "label": "精修", "description": "3 次调用，增加一次审阅修正"},
                ],
            }
        )

    async def providers(self):
        return _ok(
            {
                "providers": self._providers(),
                "default_provider_id": await self._default_provider_id(),
            }
        )

    async def start_generation(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        name = _clip(body.get("name"), 80)
        material = str(body.get("material") or "").strip()
        notes = str(body.get("notes") or "").strip()[:5000]
        if not name:
            return _error("请先填写角色名")
        if not material and not notes:
            return _error("请至少填写角色资料或补充要求")
        source_type = str(body.get("source_type") or "original").strip().lower()
        if source_type not in SOURCE_TYPES:
            source_type = "original"
        depth = str(body.get("depth") or "standard").strip().lower()
        if depth not in DEPTHS:
            depth = "standard"
        provider_id = str(body.get("provider_id") or "").strip()
        if not provider_id:
            provider_id = await self._default_provider_id()
        if not provider_id:
            return _error("AstrBot 还没有配置可用的对话模型")
        if provider_id not in {item["id"] for item in self._providers()}:
            return _error("选择的模型不存在或已停用")

        max_chars = self._max_source_chars()
        truncated = len(material) > max_chars
        if truncated:
            material = material[:max_chars]

        async with self._job_lock:
            self._cleanup_jobs()
            if any(job.get("status") == "running" for job in self.jobs.values()):
                return _error("已有一个人格制作任务在运行，请等它完成")
            job_id = uuid.uuid4().hex[:12]
            job = {
                "job_id": job_id,
                "status": "running",
                "stage": "queued",
                "progress": 0,
                "message": "准备中…",
                "created_at": _now(),
                "created_ts": time.time(),
                "truncated": truncated,
                "payload": {
                    "name": name,
                    "source_type": source_type,
                    "material": material,
                    "notes": notes,
                    "depth": depth,
                    "provider_id": provider_id,
                },
            }
            self.jobs[job_id] = job
            task = asyncio.create_task(self._run_generation(job_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return _ok({"job_id": job_id, "truncated": truncated})

    async def _run_generation(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        payload = job["payload"]

        def progress(stage: str, message: str, percent: int) -> None:
            job["stage"] = stage
            job["message"] = message
            job["progress"] = percent

        try:
            generator = PersonaGenerator(
                self.context,
                provider_id=payload["provider_id"],
                depth=payload["depth"],
                progress=progress,
            )
            result = await generator.generate(
                name=payload["name"],
                source_type=payload["source_type"],
                material=payload["material"],
                notes=payload["notes"],
            )
            job["status"] = "done"
            job["stage"] = "done"
            job["progress"] = 100
            job["message"] = "人格已生成"
            job["finished_at"] = _now()
            job["result"] = result
            logger.info(
                "[persona_workshop] 人格生成完成：%s（%s）",
                payload["name"],
                payload["provider_id"],
            )
        except PersonaGenerationError as exc:
            job["status"] = "error"
            job["message"] = str(exc)
            job["finished_at"] = _now()
            logger.warning("[persona_workshop] 人格生成失败：%s", exc)
        except Exception as exc:
            job["status"] = "error"
            job["message"] = f"生成失败：{exc}"
            job["finished_at"] = _now()
            logger.exception("[persona_workshop] 人格生成异常")

    async def generation_status(self):
        job_id = str(request.query.get("job_id") or "").strip()
        job = self.jobs.get(job_id)
        if not job:
            return _error("任务不存在或已过期，请重新开始")
        data = {
            "job_id": job["job_id"],
            "status": job["status"],
            "stage": job["stage"],
            "progress": job["progress"],
            "message": job["message"],
            "created_at": job["created_at"],
            "truncated": job.get("truncated", False),
        }
        if job["status"] == "done":
            data["result"] = job.get("result")
        return _ok(data)

    async def persona_detail(self):
        persona_id = str(request.query.get("id") or "").strip()
        persona = self.store.get(persona_id)
        if not persona:
            return _error("人格不存在")
        return _ok({"persona": self._persona_detail(persona), "active": persona.get("native_persona_id") == self._active_persona_id()})

    async def compile_prompt(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        persona = normalize_persona(body.get("persona") or {})
        prompt = build_system_prompt(persona)
        return _ok({"system_prompt": prompt})

    async def save_persona(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        raw = body.get("persona")
        if not isinstance(raw, dict):
            return _error("人格数据格式错误")
        persona = normalize_persona(raw)
        if not persona["name"]:
            return _error("人格名不能为空")
        persona_id = str(persona.get("id") or "").strip() or uuid.uuid4().hex[:12]
        existing = self.store.get(persona_id)
        now = _now()
        if existing:
            persona["created_at"] = existing.get("created_at") or now
            persona["native_persona_id"] = (
                persona.get("native_persona_id")
                or existing.get("native_persona_id")
                or ""
            )
            persona["provider_id"] = persona.get("provider_id") or existing.get("provider_id", "")
            persona["model"] = persona.get("model") or existing.get("model", "")
            if not persona.get("facts"):
                persona["facts"] = existing.get("facts", [])
        else:
            persona["created_at"] = now
        persona["id"] = persona_id

        system_prompt = str(body.get("system_prompt") or "").strip()
        if not system_prompt:
            system_prompt = build_system_prompt(persona)
        if len(system_prompt) > MAX_PROMPT_CHARS:
            return _error(f"系统提示词超过 {MAX_PROMPT_CHARS} 字，请精简后再保存")
        try:
            native_id = await self._sync_native(persona, system_prompt)
        except Exception as exc:
            logger.exception("[persona_workshop] 同步原生人格失败")
            return _error(f"写入 AstrBot 人格失败：{exc}")
        persona["native_persona_id"] = native_id
        persona["system_prompt"] = system_prompt
        persona["updated_at"] = now

        source_text = body.get("source_text")
        if isinstance(source_text, str) and source_text.strip():
            self.store.write_source(persona_id, source_text[: self._max_source_chars()])
        self.store.upsert(persona)
        logger.info("[persona_workshop] 已保存人格：%s（%s）", persona["name"], native_id)
        return _ok(
            {
                "persona": self._persona_detail(persona),
                "active": native_id == self._active_persona_id(),
            }
        )

    async def use_persona(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        persona_id = str(body.get("id") or "").strip()
        persona = self.store.get(persona_id)
        if not persona:
            return _error("人格不存在")
        native_id = str(persona.get("native_persona_id") or "").strip()
        if not native_id or not self._native_exists(native_id):
            system_prompt = str(persona.get("system_prompt") or "").strip()
            if not system_prompt:
                system_prompt = build_system_prompt(persona)
            try:
                native_id = await self._sync_native(persona, system_prompt)
            except Exception as exc:
                return _error(f"写入 AstrBot 人格失败：{exc}")
            persona["native_persona_id"] = native_id
            persona["system_prompt"] = system_prompt
            persona["updated_at"] = _now()
            self.store.upsert(persona)
        try:
            self._set_active_persona(native_id)
        except Exception as exc:
            logger.exception("[persona_workshop] 设置默认人格失败")
            return _error(f"设置默认人格失败：{exc}")
        logger.info("[persona_workshop] 已切换到人格：%s", native_id)
        return _ok({"active_persona_id": native_id})

    async def delete_persona(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        persona_id = str(body.get("id") or "").strip()
        persona = self.store.get(persona_id)
        if not persona:
            return _error("人格不存在")
        native_id = str(persona.get("native_persona_id") or "").strip()
        if native_id and native_id == self._active_persona_id():
            return _error("这个人格正在使用中，请先切换到其他人格再删除")
        if native_id and self._native_exists(native_id):
            try:
                await self.context.persona_manager.delete_persona(native_id)
            except Exception as exc:
                logger.exception("[persona_workshop] 删除原生人格失败")
                return _error(f"删除 AstrBot 人格失败：{exc}")
        self.store.delete(persona_id)
        logger.info("[persona_workshop] 已删除人格：%s", persona_id)
        return _ok({"deleted": persona_id})

    async def _get_continuum(self):
        if self._continuum is not None:
            return self._continuum
        async with self._continuum_lock:
            if self._continuum is None:
                from .continuum_runtime import ContinuumRuntime

                data_dir = Path(StarTools.get_data_dir(NAME)) / "continuum"
                self._continuum = await asyncio.to_thread(
                    ContinuumRuntime,
                    self.context,
                    data_dir,
                )
                logger.info("[persona_workshop] persona-continuum 制作子系统已启动：%s", data_dir)
        return self._continuum

    async def terminate(self) -> None:
        runtime = self._continuum
        self._continuum = None
        self._room_runtime = None
        self._pending_room_turns.clear()
        if runtime is not None:
            await asyncio.to_thread(runtime.close)
            logger.info("[persona_workshop] persona-continuum 制作子系统已关闭")

    async def _get_room_runtime(self):
        if not self._bool("room_runtime_enabled", True):
            return None
        if self._room_runtime is not None:
            return self._room_runtime
        async with self._room_runtime_lock:
            if self._room_runtime is None:
                continuum = await self._get_continuum()
                from .room_runtime import RoomRuntimeManager

                self._room_runtime = await asyncio.to_thread(
                    RoomRuntimeManager,
                    continuum,
                    self.context,
                    self.conf,
                    self._load_continuum_links,
                    Path(StarTools.get_data_dir(NAME)),
                )
                logger.info("[persona_workshop] Room runtime 已启动")
        return self._room_runtime

    async def continuum_runtime_status(self):
        try:
            from .livingmemory_bridge import LivingMemoryBridge

            manager = self._room_runtime
            living = LivingMemoryBridge(self.context)
            sessions = 0
            if manager is not None:
                sessions = len(manager._sessions)
            else:
                try:
                    sessions = len(json.loads((Path(StarTools.get_data_dir(NAME)) / "runtime_sessions.json").read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    sessions = 0
            return _ok(
                {
                    "room_runtime_enabled": self._bool("room_runtime_enabled", True),
                    "memory_mode": str(self.conf.get("memory_mode", "auto")),
                    "livingmemory_available": living.available,
                    "room_sessions": sessions,
                    "pending_turns": len(self._pending_room_turns),
                }
            )
        except Exception as exc:
            return _error(f"读取运行时状态失败：{exc}")

    def _native_to_continuum(self, native_persona_id: str) -> str:
        for continuum_id, native_id in self._load_continuum_links().items():
            if native_id == native_persona_id:
                return continuum_id
        return ""

    def _pending_turn_for_event(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        turn = self._pending_room_turns.get(id(event))
        if turn is not None:
            return turn
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        for item in self._pending_room_turns.values():
            if item.get("umo") == umo:
                return item
        return None

    @filter.llm_tool(name="persona_search_memories")
    async def persona_search_memories_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 5,
    ) -> str:
        """Search this persona's long-term memories and past events. Use at most once or twice for one reply; prefer focused keywords over the full user message.

        Args:
            query(string): Focused recall keywords, named entities, preferences, commitments or past events.
            limit(number): Maximum number of memories to return, default 5.
        """
        turn = self._pending_turn_for_event(event)
        if turn is None:
            return json.dumps(
                {"ok": False, "error": "当前没有处于人格工坊 Room 回合"},
                ensure_ascii=False,
            )
        try:
            manager = await self._get_room_runtime()
            if manager is None:
                return json.dumps({"ok": False, "error": "Room runtime 未启用"}, ensure_ascii=False)
            limit = max(1, min(20, int(limit or 5)))
            if manager.livingmemory.available and manager._memory_mode() in {"auto", "livingmemory"}:
                items = await manager.livingmemory.search(
                    query,
                    session_id=str(event.unified_msg_origin),
                    k=limit,
                )
                return json.dumps(
                    {"ok": True, "source": "livingmemory", "count": len(items), "memories": items},
                    ensure_ascii=False,
                )
            memories = manager.runtime.continuum.memories.search_memories(
                persona_id=turn["persona_id"],
                query=query,
                limit=limit,
                branch_id="main",
            )
            return json.dumps(
                {
                    "ok": True,
                    "source": "continuum",
                    "count": len(memories),
                    "memories": [
                        {
                            "id": item.id,
                            "content": item.content,
                            "type": item.type.value,
                            "importance": item.importance,
                        }
                        for item in memories
                    ],
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.exception("[persona_workshop] persona_search_memories 失败")
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="persona_get_relationship")
    async def persona_get_relationship_tool(self, event: AstrMessageEvent, counterpart: str = "") -> str:
        """Inspect the persona's current relationship state with a counterpart. Use this when the user asks about trust, closeness, familiarity, or how the persona currently feels about them.

        Args:
            counterpart(string): Name or ID of the counterpart; leave empty to use the current conversation counterpart.
        """
        turn = self._pending_turn_for_event(event)
        if turn is None:
            return json.dumps(
                {"ok": False, "error": "当前没有处于人格工坊 Room 回合"},
                ensure_ascii=False,
            )
        try:
            manager = await self._get_room_runtime()
            if manager is None:
                return json.dumps({"ok": False, "error": "Room runtime 未启用"}, ensure_ascii=False)
            target = str(counterpart or f"room:{turn['room_id']}").strip()
            state = manager.runtime.continuum.relationships.get_relationship(
                turn["persona_id"],
                target,
                branch_id="main",
            )
            return json.dumps(
                {"ok": True, "counterpart": target, "relationship": state.model_dump(mode="json")},
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.exception("[persona_workshop] persona_get_relationship 失败")
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="persona_get_runtime_state")
    async def persona_get_runtime_state_tool(self, event: AstrMessageEvent) -> str:
        """Inspect the persona's current internal emotions, active needs and goals. Use this when the user asks what the persona is feeling or why the persona reacted a certain way."""
        turn = self._pending_turn_for_event(event)
        if turn is None:
            return json.dumps(
                {"ok": False, "error": "当前没有处于人格工坊 Room 回合"},
                ensure_ascii=False,
            )
        try:
            manager = await self._get_room_runtime()
            if manager is None:
                return json.dumps({"ok": False, "error": "Room runtime 未启用"}, ensure_ascii=False)
            emotions = manager.runtime.continuum.affect.get_emotions(
                turn["persona_id"],
                "main",
            )
            needs = manager.runtime.continuum.motivation.get_needs(
                turn["persona_id"],
                "main",
            )
            return json.dumps(
                {
                    "ok": True,
                    "emotions": [
                        {"name": item.name, "intensity": item.intensity}
                        for item in emotions
                        if item.intensity > 0.01
                    ],
                    "needs": [
                        {"name": item.name, "level": item.level}
                        for item in needs
                        if item.level > 0.01
                    ],
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.exception("[persona_workshop] persona_get_runtime_state 失败")
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def _append_dynamic_context(self, req, text: str) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        parts = list(getattr(req, "extra_user_content_parts", None) or [])
        marker = "[persona_workshop_dynamic]"
        if any(marker in str(getattr(part, "text", "") or "") for part in parts):
            return True
        parts.append(TextPart(text=text).mark_as_temp())
        req.extra_user_content_parts = parts
        return True

    @filter.on_llm_request(priority=-100000)
    async def room_runtime_request(self, event: AstrMessageEvent, req) -> None:
        if not self._bool("room_runtime_enabled", True):
            return
        native_persona_id = ""
        try:
            native_persona_id = str(event.get_extra("_trace_persona_id") or "")
        except Exception:
            native_persona_id = ""
        if not native_persona_id:
            native_persona_id = self._active_persona_id()
        if not native_persona_id or not self._native_to_continuum(native_persona_id):
            return
        user_message = str(getattr(event, "message_str", "") or getattr(req, "prompt", "") or "").strip()
        if not user_message:
            return
        existing_turn = self._pending_room_turns.get(id(event))
        if existing_turn is not None:
            try:
                self._append_dynamic_context(req, existing_turn.get("dynamic_context", ""))
            except Exception:
                logger.exception("[persona_workshop] 复用 Room 动态上下文失败")
            return
        try:
            manager = await self._get_room_runtime()
            if manager is None:
                return
            turn = await manager.prepare(
                native_persona_id,
                str(event.unified_msg_origin),
                user_message,
            )
            if not self._append_dynamic_context(req, turn.get("dynamic_context", "")):
                logger.warning("[persona_workshop] Room 动态上下文为空，保留静态人格")
                return
            self._pending_room_turns[id(event)] = turn
            logger.info(
                "[persona_workshop] Room 动态上下文已注入：persona=%s，memory=%s，room=%s，chars=%s",
                turn["persona_id"],
                turn["memory_backend"],
                turn["room_id"],
                len(turn.get("dynamic_context", "")),
            )
        except Exception:
            logger.exception("[persona_workshop] Room 动态上下文准备失败，回退静态人格")
            return

    @filter.on_agent_done(priority=-100000)
    async def room_runtime_done(self, event: AstrMessageEvent, run_context, llm_response) -> None:
        turn = self._pending_room_turns.pop(id(event), None)
        if turn is None:
            return
        text = str(getattr(llm_response, "completion_text", "") or "").strip()
        if not text:
            return
        try:
            manager = await self._get_room_runtime()
            if manager is None:
                return
            await asyncio.to_thread(manager.commit, turn, text)
            logger.info(
                "[persona_workshop] Room 回合已提交：persona=%s，room=%s",
                turn["persona_id"],
                turn["room_id"],
            )
        except Exception:
            logger.exception("[persona_workshop] Room 回合提交失败")


    async def continuum_state(self):
        try:
            runtime = await self._get_continuum()
            force = str(request.query.get("refresh") or "").strip().lower() in {"1", "true", "yes"}
            agents = await runtime.scan_agents(force_refresh=force)
            jobs = runtime.creation.list_jobs(task_center=True, page_size=50)
            personas = runtime.personas.list()
            return _ok(
                {
                    "agents": [item.model_dump(mode="json") for item in agents],
                    "jobs": [item.model_dump(mode="json") for item in jobs],
                    "personas": [item.model_dump(mode="json") for item in personas],
                    "broker_capabilities": list(runtime.broker.capabilities()),
                }
            )
        except Exception as exc:
            logger.exception("[persona_workshop] continuum state 失败")
            return _error(f"读取制作子系统失败：{exc}")

    async def continuum_create_job(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        try:
            runtime = await self._get_continuum()
            job = await runtime.creation.create_job(
                display_name=str(body.get("display_name") or "").strip(),
                aliases=list(body.get("aliases") or []),
                persona_type=str(body.get("persona_type") or "fictional_or_synthetic_person"),
                creation_mode=str(body.get("creation_mode") or "fictional"),
                runtime_source=str(body.get("runtime_source") or "api"),
                agent_id=str(body.get("agent_id") or "api_astrbot"),
                model_id=str(body.get("model_id") or ""),
                reasoning_effort=body.get("reasoning_effort"),
                research_policy=body.get("research_policy"),
                birth_date=body.get("birth_date"),
                death_date=body.get("death_date"),
                data_cutoff_date=body.get("data_cutoff_date"),
                existing_persona_id=body.get("existing_persona_id"),
                duplicate_action=str(body.get("duplicate_action") or "enrich"),
                materials=list(body.get("materials") or []),
                remote_material_consent=bool(body.get("remote_material_consent")),
                subject_kind=body.get("subject_kind"),
                work_or_universe=body.get("work_or_universe"),
                life_status=body.get("life_status"),
                privacy_scope=body.get("privacy_scope"),
                identity_context=body.get("identity_context"),
                persona_notes=body.get("persona_notes"),
                user_defined_facts=body.get("user_defined_facts"),
                research_mode=body.get("research_mode"),
                web_scope=body.get("web_scope"),
                research_instructions=body.get("research_instructions"),
                job_config=(
                    {
                        **(
                            {"material_semantic_gate_mode": body.get("material_semantic_gate_mode")}
                            if body.get("material_semantic_gate_mode")
                            else {}
                        ),
                        **({"force_revalidate": True} if body.get("force_revalidate") else {}),
                    }
                    or None
                ),
                start_worker=bool(body.get("start_worker", True)),
            )
            return _ok({"job": job.model_dump(mode="json")})
        except Exception as exc:
            if type(exc).__name__ == "DuplicatePersonaError":
                return _error(
                    "persona_exists",
                    {
                        "persona_id": getattr(exc, "persona_id", ""),
                        "display_name": getattr(exc, "display_name", ""),
                    },
                )
            logger.exception("[persona_workshop] continuum create job 失败")
            return _error(f"创建制作任务失败：{exc}")

    async def continuum_job_status(self):
        job_id = str(request.query.get("job_id") or "").strip()
        if not job_id:
            return _error("缺少 job_id")
        try:
            runtime = await self._get_continuum()
            return _ok({"job": runtime.creation.get_job(job_id).model_dump(mode="json")})
        except Exception as exc:
            return _error(f"读取任务失败：{exc}")

    async def continuum_job_action(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        job_id = str(body.get("job_id") or "").strip()
        action = str(body.get("action") or "").strip().lower()
        if not job_id or not action:
            return _error("缺少 job_id 或 action")
        try:
            runtime = await self._get_continuum()
            if action == "pause":
                job = await runtime.creation.pause_job(job_id)
            elif action == "resume":
                job = await runtime.creation.resume_job(job_id, runtime=body.get("runtime"))
            elif action == "cancel":
                job = await runtime.creation.cancel_job(job_id)
            elif action == "retry":
                job = await runtime.creation.retry_job(job_id, runtime=body.get("runtime"))
            elif action == "continue":
                job = await runtime.creation.continue_job(
                    job_id,
                    research_policy=body.get("research_policy"),
                    requested_scope=body.get("requested_scope"),
                    materials=list(body.get("materials") or []),
                    runtime=body.get("runtime"),
                    enrichment_input_mode=body.get("enrichment_input_mode"),
                )
            elif action == "interview":
                job = await runtime.creation.answer_interview(
                    job_id,
                    str(body.get("answer") or ""),
                    dimension=body.get("dimension"),
                )
            elif action == "materials":
                job = await runtime.creation.add_materials(
                    job_id,
                    list(body.get("materials") or []),
                    consent=bool(body.get("consent")),
                )
            else:
                return _error(f"未知操作：{action}")
            return _ok({"job": job.model_dump(mode="json")})
        except Exception as exc:
            logger.exception("[persona_workshop] continuum job action 失败")
            return _error(f"任务操作失败：{exc}")

    async def continuum_personas(self):
        try:
            runtime = await self._get_continuum()
            records = runtime.personas.list()
            links = self._load_continuum_links()
            active = self._active_persona_id()
            return _ok(
                {
                    "personas": [
                        {
                            **item.model_dump(mode="json"),
                            "native_persona_id": links.get(item.id, ""),
                            "active": links.get(item.id) == active,
                        }
                        for item in records
                    ],
                    "active_persona_id": active,
                }
            )
        except Exception as exc:
            return _error(f"读取人格失败：{exc}")

    def _continuum_links_path(self) -> Path:
        return Path(StarTools.get_data_dir(NAME)) / "continuum_links.json"

    def _load_continuum_links(self) -> dict[str, str]:
        try:
            data = json.loads(self._continuum_links_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if value}

    def _save_continuum_links(self, links: dict[str, str]) -> None:
        path = self._continuum_links_path()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(path)

    async def continuum_use_persona(self):
        body = await request.json(default={}) or {}
        persona_id = str(body.get("id") or "").strip() if isinstance(body, dict) else ""
        if not persona_id:
            return _error("缺少人格 id")
        try:
            runtime = await self._get_continuum()
            record = runtime.personas.get(persona_id)
            manifest = record.manifest
            display_name = str(manifest.display_name or manifest.id)
            from .continuum_persona_renderer import PersonaRenderer

            prompt = PersonaRenderer(
                Path(record.package_path),
                max_traits=12,
                max_dialogue_examples=10,
                max_contradictions=10,
                keep_runtime_rules=True,
                reply_length_limit=0,
            ).render()
            if not prompt.strip():
                return _error("人格包没有可用的内容，无法生成系统提示词")
            links = self._load_continuum_links()
            native_id = links.get(persona_id, "")
            if native_id and not self._native_exists(native_id):
                native_id = ""
            if not native_id:
                used = set(links.values())
                used.update(
                    str(item.get("native_persona_id") or "")
                    for item in self.store.list()
                )
                base = f"{self._native_prefix()}{display_name}"[:60]
                native_id = base
                index = 2
                while native_id in used or self._native_exists(native_id):
                    native_id = f"{base}（{index}）"[:60]
                    index += 1
            if self._native_exists(native_id):
                await self.context.persona_manager.update_persona(
                    native_id,
                    system_prompt=prompt,
                )
            else:
                await self.context.persona_manager.create_persona(
                    native_id,
                    prompt,
                    tools=None,
                    skills=None,
                )
            links[persona_id] = native_id
            self._save_continuum_links(links)
            self._set_active_persona(native_id)
            logger.info("[persona_workshop] 已启用 continuum 人格：%s -> %s", persona_id, native_id)
            return _ok({"active_persona_id": native_id, "native_persona_id": native_id})
        except Exception as exc:
            logger.exception("[persona_workshop] 启用 continuum 人格失败")
            return _error(f"启用人格失败：{exc}")

    async def continuum_upload_material(self):
        import tempfile

        try:
            files = await request.files()
            upload = files.get("file")
            if upload is None:
                return _error("没有收到文件")
            runtime = await self._get_continuum()
            suffix = Path(upload.filename or "material.bin").suffix[:20]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                temp_path = Path(handle.name)
            try:
                await upload.save(temp_path)
                async def chunks():
                    with temp_path.open("rb") as source:
                        while True:
                            piece = await asyncio.to_thread(source.read, 1024 * 1024)
                            if not piece:
                                break
                            yield piece

                record = await runtime.continuum.material_uploads.astream_chunks(
                    filename=upload.filename or "material.bin",
                    chunks=chunks(),
                    content_length=upload.content_length,
                )
            finally:
                temp_path.unlink(missing_ok=True)
            return _ok(record)
        except Exception as exc:
            logger.exception("[persona_workshop] 上传人格资料失败")
            return _error(f"上传资料失败：{exc}")

    async def continuum_delete_persona(self):
        body = await request.json(default={}) or {}
        persona_id = str(body.get("id") or "").strip() if isinstance(body, dict) else ""
        if not persona_id:
            return _error("缺少人格 id")
        try:
            runtime = await self._get_continuum()
            links = self._load_continuum_links()
            native_id = links.get(persona_id, "")
            if native_id and native_id == self._active_persona_id():
                return _error("这个人格正在使用中，请先切换到其他人格再删除")
            if native_id and self._native_exists(native_id):
                await self.context.persona_manager.delete_persona(native_id)
            runtime.personas.delete(persona_id)
            links.pop(persona_id, None)
            self._save_continuum_links(links)
            return _ok({"deleted": persona_id})
        except Exception as exc:
            logger.exception("[persona_workshop] 删除 continuum 人格失败")
            return _error(f"删除人格失败：{exc}")

    async def continuum_validate_research(self):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _error("请求格式错误")
        runtime_payload = {
            "runtime_source": str(body.get("runtime_source") or "local_cli"),
            "agent_id": str(body.get("agent_id") or ""),
            "model_id": str(body.get("model_id") or ""),
            "reasoning_effort": body.get("reasoning_effort"),
            "auth_profile_id": body.get("auth_profile_id"),
        }
        if not runtime_payload["agent_id"]:
            return _error("缺少执行器")
        try:
            runtime = await self._get_continuum()
            probe = await runtime.creation.validate_research_runtime(
                runtime_payload,
                force_revalidate=True,
            )
            return _ok({"probe": probe.model_dump(mode="json")})
        except Exception as exc:
            logger.exception("[persona_workshop] 联网研究能力验证失败")
            return _error(f"联网能力验证失败：{exc}")

    async def use_native_persona(self):
        body = await request.json(default={}) or {}
        native_id = str(body.get("native_persona_id") or "").strip() if isinstance(body, dict) else ""
        if not native_id:
            return _error("缺少 native_persona_id")
        if not self._native_exists(native_id):
            return _error("AstrBot 人格不存在")
        try:
            self._set_active_persona(native_id)
        except Exception as exc:
            return _error(f"设置默认人格失败：{exc}")
        return _ok({"active_persona_id": native_id})
