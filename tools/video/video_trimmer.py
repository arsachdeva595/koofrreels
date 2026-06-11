from __future__ import annotations

import subprocess
from pathlib import Path

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class VideoTrimmer(BaseTool):
    name = "video_trimmer"
    version = "0.1.0"
    tier = ToolTier.PROCESS
    capability = "trim_clip"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["input_path", "output_path", "trim_in", "trim_out"],
        "properties": {
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "trim_in": {"type": "number"},
            "trim_out": {"type": "number"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        input_path = params["input_path"]
        output_path = params["output_path"]
        trim_in = params["trim_in"]
        trim_out = params["trim_out"]
        duration = trim_out - trim_in

        if duration <= 0:
            return ToolResult(success=False, error=f"trim_in ({trim_in}) >= trim_out ({trim_out})")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(trim_in),
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr[-800:])

        if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
            return ToolResult(
                success=False,
                error=f"FFmpeg exited 0 but trimmed file not created at {output_path}. stderr: {result.stderr[-400:]}",
            )

        return ToolResult(
            success=True,
            data={"output_path": output_path, "duration_seconds": duration},
        )
