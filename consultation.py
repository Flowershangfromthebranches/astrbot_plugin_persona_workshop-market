from __future__ import annotations

import asyncio
from typing import Any

from astrbot import logger


class SilentConsultation:
    """Optional host + expert + synthesis pass before the main reply.

    Nothing from this pass is shown to the user.  The result is a compact
    internal brief injected into the persona context for the final model turn.
    """

    EXPERT_PROMPTS = (
        (
            "心理与关系分析专家",
            "你是人格心理与关系分析专家。只分析用户这句话背后的真实需求、情绪变化、"
            "关系动态和角色可能产生的内在反应；不要写用户回复，不要编造记忆。",
        ),
        (
            "对话策略专家",
            "你是对话策略专家。基于角色人格、当前状态和用户消息，只输出这次回复应该"
            "采取的方向、边界、禁忌、节奏和需要避免的表达；不要写完整回复。",
        ),
        (
            "连续性与记忆审阅专家",
            "你是连续性与记忆审阅专家。只检查这次回复需要延续或避免的记忆、关系、情绪"
            "和事实边界，指出可能的前后矛盾；不要写用户回复。",
        ),
    )

    def __init__(self, context: Any, config: Any) -> None:
        self.context = context
        self.config = config

    def _mode(self) -> str:
        value = str(self.config.get("consultation_mode", "off") or "off").strip().lower()
        return value if value in {"off", "complex", "always"} else "off"

    def should_run(self, user_message: str) -> bool:
        mode = self._mode()
        if mode == "off":
            return False
        if mode == "always":
            return True
        text = str(user_message or "").strip()
        try:
            minimum = max(20, int(self.config.get("consultation_min_chars", 120) or 120))
        except (TypeError, ValueError):
            minimum = 120
        if len(text) >= minimum:
            return True
        return any(
            keyword in text
            for keyword in ("怎么办", "为什么", "分析", "建议", "纠结", "很难", "你觉得", "我该")
        )

    async def _provider_id(self) -> str:
        configured = str(self.config.get("consultation_provider_id", "") or "").strip()
        if configured:
            return configured
        try:
            provider = await self.context.get_using_provider_async()
            if provider is not None:
                return str(provider.meta().id)
        except Exception:
            pass
        return ""

    async def _ask(self, provider_id: str, system_prompt: str, prompt: str) -> str:
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                ),
                timeout=90,
            )
        except Exception as exc:
            logger.warning("[persona_workshop] 静默会诊调用失败：%s", exc)
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()

    async def run(
        self,
        *,
        persona_name: str,
        system_prompt: str,
        user_message: str,
        state_text: str,
        memories: list[str],
    ) -> str:
        if not self.should_run(user_message):
            return ""
        provider_id = await self._provider_id()
        if not provider_id:
            logger.warning("[persona_workshop] 静默会诊跳过：没有可用模型")
            return ""
        memory_block = "\n".join(f"- {item}" for item in memories[:8] if item) or "（无）"
        context = (
            f"角色：{persona_name}\n\n"
            f"角色人格与当前运行时上下文（节选）：\n{system_prompt[:3500]}\n\n"
            f"当前状态：\n{state_text or '（无）'}\n\n"
            f"相关记忆：\n{memory_block}\n\n"
            f"用户这句话：\n{user_message}"
        )
        expert_notes: list[str] = []
        for title, prompt in self.EXPERT_PROMPTS:
            note = await self._ask(provider_id, prompt, context)
            if note:
                expert_notes.append(f"### {title}\n{note[:1200]}")
        if not expert_notes:
            return ""
        synthesis_prompt = (
            "你是本次静默会诊的主持人。请把专家意见综合成一段内部备忘，不能直接当成给用户的回复。"
            "只输出以下结构，每个字段尽量短：\n"
            "用户真正需求：\n当前情绪与关系动态：\n建议回应方向：\n需要延续的记忆：\n必须避免：\n"
            "总长度不超过 500 字。不要写完整回复，不要提及会诊过程。"
        )
        brief = await self._ask(
            provider_id,
            synthesis_prompt,
            context + "\n\n专家意见：\n" + "\n\n".join(expert_notes),
        )
        return brief[:1600]
