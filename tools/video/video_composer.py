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


class VideoComposer(BaseTool):
    """Concatenate normalized clips — hard cuts via concat demuxer (fast stream copy)."""

    name = "video_composer"
    version = "0.2.0"
    tier = ToolTier.PROCESS
    capability = "compose_video"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["clips", "output_path"],
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "duration_seconds": {"type": "number"},
                        "transition": {"type": "string"},
                        "transition_duration": {"type": "number"},
                    },
                },
            },
            "output_path": {"type": "string"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        clips = params["clips"]
        output_path = params["output_path"]

        if not clips:
            return ToolResult(success=False, error="No clips provided")

        missing = [c["path"] for c in clips if not Path(c["path"]).exists()]
        if missing:
            return ToolResult(
                success=False,
                error=f"Normalized clip(s) missing before composition: {missing}",
            )

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if len(clips) == 1:
            return self._single_clip(clips[0]["path"], output_path)

        return self._concat_stream_copy(clips, output_path)

    def _single_clip(self, path: str, output_path: str) -> ToolResult:
        cmd = ["ffmpeg", "-y", "-i", path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr[-500:])
        return ToolResult(success=True, data={"output_path": output_path})

    def _concat_stream_copy(self, clips: list[dict], output_path: str) -> ToolResult:
        """Fastest path: concat demuxer with stream copy — no re-encode, no filter_complex."""
        list_path = Path(output_path).parent / "concat_list.txt"
        list_path.write_text(
            "\n".join(f"file '{c['path']}'" for c in clips) + "\n",
            encoding="utf-8",
        )

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        list_path.unlink(missing_ok=True)

        if result.returncode == 0:
            return ToolResult(success=True, data={"output_path": output_path})

        # Stream copy failed (codec mismatch) — fall back to fast re-encode
        return self._concat_reencode(clips, output_path)

    def _concat_reencode(self, clips: list[dict], output_path: str) -> ToolResult:
        """Re-encode fallback using concat filter. Tries VideoToolbox then libx264."""
        inputs = []
        for c in clips:
            inputs += ["-i", c["path"]]

        filter_complex = (
            "".join(f"[{i}:v][{i}:a]" for i in range(len(clips)))
            + f"concat=n={len(clips)}:v=1:a=1[vout][aout]"
        )

        base = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
        ]
        tail = ["-c:a", "aac", "-b:a", "128k", output_path]

        hw_cmd = base + ["-c:v", "h264_videotoolbox", "-q:v", "55"] + tail
        result = subprocess.run(hw_cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            return ToolResult(success=True, data={"output_path": output_path})

        sw_cmd = base + ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"] + tail
        result = subprocess.run(sw_cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr[-800:])
        return ToolResult(success=True, data={"output_path": output_path})
