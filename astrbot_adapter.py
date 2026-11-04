from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from .vendor_bootstrap import ensure_vendor

ensure_vendor()

from persona_continuum.agent.adapter import AgentAdapter, AgentSession  # noqa: E402
from persona_continuum.agent.context_capability import ContextWindowMode  # noqa: E402
from persona_continuum.agent.models import (  # noqa: E402
    AgentCapabilityFlags,
    AgentEvent,
    AgentEventType,
    AgentProbeResult,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    ModelCapability,
    OutputStreamingMode,
    ReasoningCapability,
    ReasoningCapabilityMode,
    SelectionStrategy,
    StructuredOutputMode,
)
from persona_continuum.agent.prompt import AgentPromptRenderer  # noqa: E402


class AstrBotProviderAdapter(AgentAdapter):
    """Expose AstrBot's configured providers as one Persona Continuum runtime."""

    adapter_id = "api_astrbot"
    name = "AstrBot 模型"
    output_streaming_mode = OutputStreamingMode.BUFFERED_FINAL
    context_window_mode = ContextWindowMode.UNKNOWN
    adapter_session_mode = "stateless"
    retry_catalog_after_scan = False
    prompt_mode = "native_roles"

    def __init__(self, astrbot_context: Any) -> None:
        self.context = astrbot_context

    def _provider_models(self) -> list[ModelCapability]:
        models: list[ModelCapability] = []
        seen: set[str] = set()
        for provider in self.context.get_all_providers():
            try:
                meta = provider.meta()
                provider_id = str(meta.id or "").strip()
            except Exception:
                continue
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            model = str(getattr(meta, "model", "") or provider_id)
            models.append(
                ModelCapability(
                    id=provider_id,
                    display_name=f"{model} · {provider_id}",
                    provider="astrbot",
                    selectable=True,
                    source="dynamic",
                    reasoning_selection=SelectionStrategy.UNSUPPORTED,
                    reasoning_capability=ReasoningCapability(
                        mode=ReasoningCapabilityMode.UNSUPPORTED,
                    ),
                )
            )
        return models

    async def probe(self) -> AgentProbeResult:
        models = self._provider_models()
        ready = bool(models)
        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=AgentStatus.READY if ready else AgentStatus.BROKEN,
            runtime_source="api",
            definition_source="plugin",
            protocols=["astrbot_provider"],
            capabilities=AgentCapabilityFlags(
                streaming=False,
                persistent_session=False,
                parallel_safe=True,
                resume_session=False,
                model_discovery=True,
                model_selection=SelectionStrategy.MANUAL,
                reasoning_discovery=False,
                reasoning_selection=SelectionStrategy.UNSUPPORTED,
                mcp=False,
                tool_calls=False,
                permission_control=False,
                cancel=True,
                structured_output_mode=StructuredOutputMode.PROMPT_ONLY,
                structured_output=True,
                auth_detection=False,
            ),
            models=models,
            status_detail="" if ready else "AstrBot 还没有配置可用的对话模型",
            context_window_mode=ContextWindowMode.UNKNOWN.value,
        )

    async def capabilities(self) -> AgentCapabilityFlags:
        return (await self.probe()).capabilities

    async def list_models(self) -> list[ModelCapability]:
        return (await self.probe()).models

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        session = AgentSession(config=config)
        session.session_data["protocol"] = "astrbot_provider"
        session.session_data["effective_model"] = config.model_id
        session.session_data["cancel_event"] = asyncio.Event()
        return session

    def build_permission_args(self, config: AgentSessionConfig) -> list[str]:
        return []

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        provider_id = str(session.config.model_id or "").strip()
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error=f"AstrBot provider 不存在或已停用：{provider_id}",
            )
            return
        messages = AgentPromptRenderer.render_for_native_roles(turn)
        if turn.expected_output is not None:
            schema = turn.expected_output
            if hasattr(schema, "model_json_schema"):
                schema = schema.model_json_schema()
            try:
                schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                schema_text = str(schema)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "请只输出一个合法 JSON 对象，不要加 Markdown 代码块或解释。"
                        "输出必须符合以下 JSON Schema：\n"
                        + schema_text
                    ),
                }
            )
        cancel_event = session.session_data.get("cancel_event")
        try:
            request = asyncio.create_task(
                provider.text_chat(prompt=None, contexts=messages)
            )
            if isinstance(cancel_event, asyncio.Event):
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, _ = await asyncio.wait(
                    {request, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and not request.done():
                    request.cancel()
                    with contextlib.suppress(Exception):
                        await request
                    yield AgentEvent(type=AgentEventType.ERROR, error="已取消")
                    return
                cancel_task.cancel()
            response = await request
        except Exception as exc:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error=f"AstrBot 模型调用失败：{type(exc).__name__}: {exc}",
            )
            return
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            yield AgentEvent(type=AgentEventType.ERROR, error="AstrBot 模型没有返回文本")
            return
        usage = getattr(response, "usage", None)
        yield AgentEvent(type=AgentEventType.CHUNK, content=text)
        yield AgentEvent(
            type=AgentEventType.DONE,
            content=text,
            metadata={"usage": usage if isinstance(usage, dict) else {}},
        )

    async def cancel(self, session: AgentSession) -> None:
        event = session.session_data.get("cancel_event")
        if isinstance(event, asyncio.Event):
            event.set()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
