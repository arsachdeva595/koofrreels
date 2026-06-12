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


class FrameSampler(BaseTool):
    name = "frame_sampler"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "sample_frames"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["video_path", "output_dir"],
        "properties": {
            "video_path": {"type": "string"},
            "output_dir": {"type": "string"},
            "num_frames": {"type": "integer", "minimum": 1, "maximum": 20},
            "timestamps": {
                "type": "array",
                "items": {"type": "number"},
            },
        },
    }

    def execute(self, params: dict) -> ToolResult:
        video_path = params["video_path"]
        output_dir = Path(params["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamps = params.get("timestamps")
        num_frames = params.get("num_frames", 4)

        if not timestamps:
            duration_cmd = [
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ]
            out = subprocess.run(duration_cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            try:
                duration = float(out.stdout.strip())
            except ValueError:
                duration = 30.0
            step = duration / (num_frames + 1)
            timestamps = [round(step * (i + 1), 2) for i in range(num_frames)]

        frame_paths = []
        for i, ts in enumerate(timestamps):
            out_path = output_dir / f"frame_{i:03d}_{ts:.1f}s.jpg"
            cmd = [
                "ffmpeg", "-nostdin", "-y", "-ss", str(ts), "-i", video_path,
                "-frames:v", "1", "-q:v", "2", str(out_path),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            if result.returncode == 0 and out_path.exists():
                frame_paths.append({"timestamp_seconds": ts, "path": str(out_path)})

        return ToolResult(
            success=bool(frame_paths),
            data={"frames": frame_paths, "count": len(frame_paths)},
            error=None if frame_paths else "No frames extracted",
        )
