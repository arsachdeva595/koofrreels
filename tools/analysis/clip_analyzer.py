from __future__ import annotations

import json
import subprocess

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class ClipAnalyzer(BaseTool):
    name = "clip_analyzer"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "analyze_clip"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["local_path"],
        "properties": {
            "local_path": {"type": "string"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        path = params["local_path"]
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", path,
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return ToolResult(success=False, error=out.stderr)
            probe = json.loads(out.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            return ToolResult(success=False, error=str(e))

        video_stream = next(
            (s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None
        )
        if not video_stream:
            return ToolResult(success=False, error="No video stream found")

        duration = float(probe.get("format", {}).get("duration", 0))
        fps_str = video_stream.get("r_frame_rate", "30/1")
        num, den = fps_str.split("/") if "/" in fps_str else (fps_str, "1")
        fps = round(int(num) / int(den), 2)

        return ToolResult(
            success=True,
            data={
                "duration_seconds": round(duration, 3),
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
                "fps": fps,
                "codec": video_stream.get("codec_name"),
                "size_bytes": int(probe.get("format", {}).get("size", 0)),
            },
        )
