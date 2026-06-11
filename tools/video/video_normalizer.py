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

TARGET_W = 1080
TARGET_H = 1920


class VideoNormalizer(BaseTool):
    """
    Trim + convert a clip to 9:16 (1080×1920) in a single FFmpeg pass.

    Accepts optional trim_in / trim_out so callers can skip the separate
    VideoTrimmer step and avoid an intermediate file.  When trim params are
    omitted the whole clip is normalized.
    """

    name = "video_normalizer"
    version = "0.3.0"
    tier = ToolTier.PROCESS
    capability = "normalize_clip"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["input_path", "output_path"],
        "properties": {
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "trim_in": {"type": "number"},
            "trim_out": {"type": "number"},
            "target_width": {"type": "integer"},
            "target_height": {"type": "integer"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        input_path = params["input_path"]
        output_path = params["output_path"]
        tw = params.get("target_width", TARGET_W)
        th = params.get("target_height", TARGET_H)
        trim_in = params.get("trim_in")
        trim_out = params.get("trim_out")

        if not Path(input_path).exists():
            return ToolResult(success=False, error=f"Input file not found: {input_path}")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Split input stream so each branch gets an independent copy.
        # bg branch: scale to fill 9:16, then blur
        # fg branch: scale to fit inside 9:16, padded with black bars
        # Final: overlay fg centered on bg
        vf = (
            f"[0:v]split=2[bg_in][fg_in];"
            f"[bg_in]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},boxblur=20:20[bg];"
            f"[fg_in]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black[fg];"
            f"[bg][fg]overlay=0:0,setsar=1[out]"
        )

        cmd = ["ffmpeg", "-y"]

        # Seek before -i for fast, accurate trimming via re-encode
        if trim_in is not None and trim_in > 0:
            cmd += ["-ss", str(round(trim_in, 3))]

        cmd += ["-i", input_path]

        if trim_in is not None and trim_out is not None:
            duration = round(trim_out - trim_in, 3)
            if duration <= 0:
                return ToolResult(success=False, error=f"trim_in ({trim_in}) >= trim_out ({trim_out})")
            cmd += ["-t", str(duration)]

        cmd += [
            "-ignore_unknown",   # skip Apple mebx / metadata streams in iPhone MOVs
            "-filter_complex", vf,
            "-map", "[out]",
            "-map", "0:a:0?",   # only first audio stream, ignore metadata tracks
            "-c:v", "libx264",
            "-preset", "ultrafast",  # intermediate file — nobody sees it, re-encoded in compose
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-r", "30",
            "-movflags", "+faststart",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr[-1000:])

        p = Path(output_path)
        if not p.exists() or p.stat().st_size == 0:
            return ToolResult(
                success=False,
                error=f"FFmpeg exited 0 but output missing/empty at {output_path}. "
                      f"stderr tail: {result.stderr[-500:]}",
            )

        return ToolResult(
            success=True,
            data={"output_path": output_path, "resolution": f"{tw}x{th}"},
        )
