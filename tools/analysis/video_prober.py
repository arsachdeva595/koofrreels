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


class VideoProber(BaseTool):
    name = "video_prober"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "probe_video"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        path = params["path"]
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", path,
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return ToolResult(success=False, error=f"ffprobe failed: {out.stderr}")
            probe = json.loads(out.stdout)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
        audio = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)
        fmt = probe.get("format", {})

        fps_str = (video or {}).get("r_frame_rate", "30/1")
        num, den = fps_str.split("/") if "/" in fps_str else (fps_str, "1")
        fps = round(int(num) / int(den), 2)

        return ToolResult(
            success=True,
            data={
                "codec": (video or {}).get("codec_name"),
                "width": (video or {}).get("width"),
                "height": (video or {}).get("height"),
                "fps": fps,
                "duration_seconds": round(float(fmt.get("duration", 0)), 3),
                "file_size_bytes": int(fmt.get("size", 0)),
                "audio_codec": (audio or {}).get("codec_name"),
                "audio_channels": (audio or {}).get("channels"),
                "audio_sample_rate": (audio or {}).get("sample_rate"),
                "is_playable": video is not None,
                "resolution": f"{(video or {}).get('width')}x{(video or {}).get('height')}",
            },
        )
