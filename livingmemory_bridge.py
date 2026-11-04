from __future__ import annotations

from typing import Any

from astrbot import logger
from astrbot.core.star.star import star_registry


class LivingMemoryBridge:
    """Read LivingMemory through its live plugin instance when available."""

    PLUGIN_NAME = "astrbot_plugin_livingmemory"

    def __init__(self, context: Any) -> None:
        self.context = context

    def engine(self):
        for meta in star_registry:
            if meta.name != self.PLUGIN_NAME or not meta.activated:
                continue
            instance = meta.star_cls
            if instance is None:
                continue
            initializer = getattr(instance, "initializer", None)
            engine = getattr(initializer, "memory_engine", None)
            if engine is not None:
                return engine
        return None

    @property
    def available(self) -> bool:
        return self.engine() is not None

    async def search(
        self,
        query: str,
        *,
        session_id: str,
        k: int = 5,
    ) -> list[dict[str, Any]]:
        engine = self.engine()
        if engine is None or not query.strip():
            return []
        try:
            results = await engine.search_memories(
                query=query,
                k=max(1, min(20, k)),
                session_id=session_id,
                persona_id=None,
            )
        except Exception as exc:
            logger.warning("[persona_workshop] LivingMemory 检索失败：%s", exc)
            return []
        items = []
        for result in results or []:
            content = str(getattr(result, "content", "") or "").strip()
            if not content:
                continue
            items.append(
                {
                    "content": content,
                    "score": getattr(result, "final_score", None),
                    "metadata": getattr(result, "metadata", {}) or {},
                }
            )
        return items

    @staticmethod
    def format_block(items: list[dict[str, Any]]) -> str:
        if not items:
            return ""
        lines = []
        for item in items:
            score = item.get("score")
            prefix = f"[{score:.3f}] " if isinstance(score, (int, float)) else ""
            lines.append(f"- {prefix}{item['content']}")
        return "## 长期记忆（LivingMemory）\n" + "\n".join(lines)
