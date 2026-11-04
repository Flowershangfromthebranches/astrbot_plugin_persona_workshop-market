from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - dependency is declared for the plugin
    BeautifulSoup = None

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class AstrBotResearchBroker:
    """Research broker that reuses AstrBot's configured search provider."""

    def __init__(self, astrbot_context: Any) -> None:
        self.context = astrbot_context

    def _settings(self) -> dict[str, Any]:
        try:
            config = self.context.get_config()
            settings = config.get("provider_settings") or {}
        except Exception:
            return {}
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def _keys(settings: dict[str, Any], name: str) -> list[str]:
        value = settings.get(name)
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _provider(self) -> str:
        settings = self._settings()
        if not settings.get("web_search"):
            return ""
        provider = str(settings.get("websearch_provider") or "").strip().lower()
        if provider == "tavily" and self._keys(settings, "websearch_tavily_key"):
            return "tavily"
        if provider == "bocha" and self._keys(settings, "websearch_bocha_key"):
            return "bocha"
        return ""

    def capabilities(self) -> Iterable[str]:
        capabilities = ["web_fetch"]
        if self._provider():
            capabilities.append("web_search")
        return capabilities

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        provider = self._provider()
        if provider == "tavily":
            return await self._tavily_search(query, limit)
        if provider == "bocha":
            return await self._bocha_search(query, limit)
        raise RuntimeError(
            "AstrBot 未启用可用的联网搜索。请在 AstrBot 的模型设置中启用 "
            "Web Search，并配置 Tavily 或 Bocha 的 API Key；"
            "本地资料/引导访谈模式不受影响。"
        )

    async def fetch(self, url: str) -> dict[str, Any] | str:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and "xml" not in content_type:
            text = response.text
            title = ""
        elif BeautifulSoup is None:
            text = response.text
            title = ""
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
                node.decompose()
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            text = soup.get_text("\n", strip=True)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())[:300000]
        if not text:
            raise RuntimeError(f"抓取页面没有可读文本：{url}")
        return {
            "url": url,
            "title": title,
            "text": text,
            "content": text,
            "citation": url,
            "source_identity": url,
        }

    async def _tavily_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        settings = self._settings()
        keys = self._keys(settings, "websearch_tavily_key")
        last_error: Exception | None = None
        for key in keys:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        "https://api.tavily.com/search",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "query": query,
                            "max_results": max(1, min(20, limit)),
                            "search_depth": "advanced",
                            "include_answer": False,
                            "include_raw_content": False,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                results = []
                for item in payload.get("results", []):
                    url = str(item.get("url") or "").strip()
                    title = str(item.get("title") or "").strip()
                    content = str(item.get("content") or item.get("snippet") or "").strip()
                    if not url or not content:
                        continue
                    results.append(self._result(url, title, content))
                return results
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Tavily 搜索失败：{last_error or '未配置 Key'}")

    async def _bocha_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        settings = self._settings()
        keys = self._keys(settings, "websearch_bocha_key")
        last_error: Exception | None = None
        for key in keys:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        "https://api.bochaai.com/v1/web-search",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "query": query,
                            "summary": True,
                            "count": max(1, min(50, limit)),
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                rows = (
                    payload.get("data", {})
                    .get("webPages", {})
                    .get("value", [])
                )
                results = []
                for item in rows:
                    url = str(item.get("url") or "").strip()
                    title = str(item.get("name") or "").strip()
                    content = str(item.get("snippet") or item.get("summary") or "").strip()
                    if not url or not content:
                        continue
                    results.append(self._result(url, title, content))
                return results
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Bocha 搜索失败：{last_error or '未配置 Key'}")

    @staticmethod
    def _result(url: str, title: str, content: str) -> dict[str, Any]:
        publisher = urlsplit(url).netloc
        return {
            "url": url,
            "title": title or publisher,
            "content": content,
            "snippet": content,
            "publisher": publisher,
            "category": "web",
            "citation": url,
            "source_identity": url,
        }
