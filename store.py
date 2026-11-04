from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any


class PersonaStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources = self.root / "sources"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "personas.json"
        self._lock = threading.Lock()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "personas": {}}

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict) or not isinstance(data.get("personas"), dict):
            return self._empty()
        return data

    def _write(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.chmod(0o600)
        temp.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            personas = self._read()["personas"]
        return sorted(
            personas.values(),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def get(self, persona_id: str) -> dict[str, Any] | None:
        if not persona_id:
            return None
        with self._lock:
            persona = self._read()["personas"].get(persona_id)
        return dict(persona) if isinstance(persona, dict) else None

    def upsert(self, persona: dict[str, Any]) -> dict[str, Any]:
        persona_id = str(persona.get("id") or "").strip()
        if not persona_id:
            raise ValueError("人格缺少 id")
        with self._lock:
            data = self._read()
            data["personas"][persona_id] = persona
            self._write(data)
        return persona

    def delete(self, persona_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            persona = data["personas"].pop(persona_id, None)
            if persona is not None:
                self._write(data)
        self._source_path(persona_id).unlink(missing_ok=True)
        return dict(persona) if isinstance(persona, dict) else None

    def _source_path(self, persona_id: str) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", persona_id)[:80]
        return self.sources / f"{safe_id}.txt"

    def write_source(self, persona_id: str, text: str) -> None:
        if not text:
            return
        path = self._source_path(persona_id)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def read_source(self, persona_id: str) -> str:
        try:
            return self._source_path(persona_id).read_text(encoding="utf-8")
        except OSError:
            return ""

    def source_excerpt(self, persona_id: str, limit: int = 400) -> str:
        text = self.read_source(persona_id)
        return text[:limit].strip()
