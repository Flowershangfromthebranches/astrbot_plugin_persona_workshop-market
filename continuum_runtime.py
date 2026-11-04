from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .vendor_bootstrap import ensure_vendor

ensure_vendor()


def _purge_vendor_modules() -> None:
    """Reload vendored persona-continuum code after a plugin reload."""
    for name in list(sys.modules):
        if name == "persona_continuum" or name.startswith("persona_continuum."):
            sys.modules.pop(name, None)


class ContinuumRuntime:
    """Own the embedded persona-continuum service graph for this plugin."""

    def __init__(self, astrbot_context: Any, data_dir: Path) -> None:
        self.context = astrbot_context
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._previous_home = os.environ.get("PERSONA_CONTINUUM_HOME")
        os.environ["PERSONA_CONTINUUM_HOME"] = str(self.data_dir)

        _purge_vendor_modules()
        from .astrbot_adapter import AstrBotProviderAdapter
        from .research_broker import AstrBotResearchBroker
        from persona_continuum.application.container import PersonaContinuum
        from persona_continuum.config import Config

        self.config = Config(data_dir=self.data_dir)
        self.broker = AstrBotResearchBroker(astrbot_context)
        self.continuum = PersonaContinuum(
            config=self.config,
            research_broker=self.broker,
        )
        self.continuum.init()
        self.adapter = AstrBotProviderAdapter(astrbot_context)
        # Keep the original persona-continuum CLI adapters available alongside
        # the AstrBot provider adapter.  The creation UI chooses an executor.
        self.continuum.agent_registry.register_adapter(self.adapter)
        self._install_research_fetch_hook()
        self.continuum.agent_discovery._cached_probes.clear()

    def _install_research_fetch_hook(self) -> None:
        async def independent_fetch(url: str, runtime: dict | None = None):
            return await self.broker.fetch(url)

        for adapter in self.continuum.agent_registry.get_all_adapters():
            if adapter.adapter_id == self.adapter.adapter_id:
                continue
            if callable(getattr(adapter, "research_fetch", None)):
                continue
            setattr(adapter, "research_fetch", independent_fetch)

    async def scan_agents(self, force_refresh: bool = False):
        return await self.continuum.agent_discovery.scan(force_refresh=force_refresh)

    @property
    def creation(self):
        return self.continuum.persona_creation

    @property
    def personas(self):
        return self.continuum.personas

    @property
    def database(self):
        return self.continuum.database

    def close(self) -> None:
        try:
            self.continuum.close()
        finally:
            if self._previous_home is None:
                os.environ.pop("PERSONA_CONTINUUM_HOME", None)
            else:
                os.environ["PERSONA_CONTINUUM_HOME"] = self._previous_home
