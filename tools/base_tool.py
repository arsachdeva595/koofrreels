from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolTier(str, Enum):
    CORE = "core"
    SOURCE = "source"
    ANALYZE = "analyze"
    PROCESS = "process"
    AI = "ai"
    DELIVER = "deliver"


class ToolStability(str, Enum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    PRODUCTION = "production"


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class Determinism(str, Enum):
    DETERMINISTIC = "deterministic"
    SEEDED = "seeded"
    STOCHASTIC = "stochastic"


class ToolRuntime(str, Enum):
    LOCAL = "local"
    API = "api"
    HYBRID = "hybrid"


class ToolStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


@dataclass
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    status: ToolStatus = ToolStatus.AVAILABLE


class BaseTool(ABC):
    name: str = ""
    version: str = "0.1.0"
    tier: ToolTier = ToolTier.CORE
    capability: str = ""
    provider: str = ""
    stability: ToolStability = ToolStability.PRODUCTION
    execution_mode: ExecutionMode = ExecutionMode.SYNC
    determinism: Determinism = Determinism.DETERMINISTIC
    runtime: ToolRuntime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    input_schema: dict = {}
    output_schema: dict = {}

    def run(self, params: dict) -> ToolResult:
        start = time.perf_counter()
        result = self.execute(params)
        result.duration_seconds = time.perf_counter() - start
        return result

    @abstractmethod
    def execute(self, params: dict) -> ToolResult:
        ...

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def health_check(self) -> dict:
        return {"status": self.get_status(), "name": self.name, "version": self.version}

    def estimate_cost(self, params: dict) -> float:
        return 0.0

    def describe(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "tier": self.tier,
            "capability": self.capability,
            "provider": self.provider,
            "stability": self.stability,
            "runtime": self.runtime,
            "status": self.get_status(),
        }
