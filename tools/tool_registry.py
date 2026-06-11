from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Type

from tools.base_tool import BaseTool, ToolStatus, ToolTier


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def discover_all(self) -> None:
        tools_dir = Path(__file__).parent
        for pkg in ["koofr", "analysis", "video", "audio", "ai"]:
            pkg_path = tools_dir / pkg
            if not pkg_path.exists():
                continue
            for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
                module = importlib.import_module(f"tools.{pkg}.{module_name}")
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseTool)
                        and attr is not BaseTool
                        and attr.name
                    ):
                        self.register(attr())

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_tools(
        self,
        tier: ToolTier | None = None,
        status: ToolStatus | None = None,
    ) -> list[BaseTool]:
        result = list(self._tools.values())
        if tier:
            result = [t for t in result if t.tier == tier]
        if status:
            result = [t for t in result if t.get_status() == status]
        return result

    def report(self) -> list[dict]:
        return [t.describe() for t in self._tools.values()]
