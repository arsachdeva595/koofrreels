from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
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

_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
_FONT_FALLBACK = "/System/Library/Fonts/Supplemental/Impact.ttf"
_WRAP_CHARS = 22  # max chars per line at default font size for 1080p vertical


def _font() -> str:
    return _FONT_PATH if Path(_FONT_PATH).exists() else _FONT_FALLBACK


def _wrap(text: str, max_chars: int = _WRAP_CHARS) -> str:
    """Word-wrap and return text with real newlines (for writing to a file)."""
    lines = textwrap.wrap(text, width=max_chars, break_long_words=False)
    return "\n".join(lines) if lines else text


def _escape_filter_path(path: str) -> str:
    """Escape a file path for use inside an FFmpeg filter option value."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


class TextRenderer(BaseTool):
    """Burn per-scene timed text overlays onto a video using FFmpeg drawtext."""

    name = "text_renderer"
    version = "0.4.0"
    tier = ToolTier.PROCESS
    capability = "render_text"
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
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "position": {"type": "string"},
                    },
                },
            },
            # Legacy single title/caption (still supported)
            "text_plan": {"type": "object"},
            "font_size": {"type": "number"},
            "color": {"type": "string"},
            "stroke_color": {"type": "string"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        input_path = params["input_path"]
        output_path = params["output_path"]
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        font_size = int(params.get("font_size", 56))
        font = _font()

        scenes = params.get("scenes") or []

        if not scenes and params.get("text_plan"):
            tp = params["text_plan"]
            title = tp.get("title", "")
            caption = tp.get("caption")
            if title:
                scenes.append({"text": title, "start": 0, "end": 99999, "position": "top"})
            if caption:
                scenes.append({"text": caption, "start": 0, "end": 99999, "position": "bottom"})

        if not scenes:
            cmd = ["ffmpeg", "-nostdin", "-y", "-i", input_path, "-c", "copy", output_path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                return ToolResult(success=False, error=r.stderr[-500:])
            return ToolResult(success=True, data={"output_path": output_path})

        tmpfiles: list[str] = []
        filters = []

        try:
            for s in scenes:
                text = s.get("text", "").strip()
                if not text:
                    continue
                t_start = s.get("start", 0)
                t_end = s.get("end", 99999)
                position = s.get("position", "bottom")

                # Write wrapped text to a temp file — avoids all escape-sequence
                # issues with inline drawtext text= option (\n interpreted as n by FFmpeg)
                fd, txt_path = tempfile.mkstemp(suffix=".txt", prefix="kfr_overlay_")
                tmpfiles.append(txt_path)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(_wrap(text))

                is_top = position == "top"
                size = font_size if is_top else max(34, int(font_size * 0.72))
                y_expr = "h*0.07" if is_top else "h*0.80"
                fade = 0.35

                alpha = (
                    f"if(lt(t-{t_start},{fade}),(t-{t_start})/{fade},"
                    f"if(lt({t_end}-t,{fade}),({t_end}-t)/{fade},1))"
                )
                enable = f"between(t,{t_start},{t_end})"

                filters.append(
                    f"drawtext=textfile='{_escape_filter_path(txt_path)}'"
                    f":fontfile='{_escape_filter_path(font)}'"
                    f":fontsize={size}"
                    f":fontcolor=white"
                    f":box=1:boxcolor=0x000000@0.88:boxborderw=14"
                    f":line_spacing=6"
                    f":x=(w-text_w)/2:y={y_expr}"
                    f":alpha='{alpha}'"
                    f":enable='{enable}'"
                )

            if not filters:
                cmd = ["ffmpeg", "-nostdin", "-y", "-i", input_path, "-c", "copy", output_path]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    return ToolResult(success=False, error=r.stderr[-500:])
                return ToolResult(success=True, data={"output_path": output_path})

            vf = ",".join(filters)

            for enc in [
                ["-c:v", "h264_videotoolbox", "-q:v", "60"],
                ["-c:v", "libx264", "-preset", "fast", "-crf", "23"],
            ]:
                cmd = [
                    "ffmpeg", "-nostdin", "-y", "-i", input_path,
                    "-vf", vf,
                    *enc,
                    "-c:a", "copy",
                    output_path,
                ]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, stdin=subprocess.DEVNULL)
                if r.returncode == 0:
                    return ToolResult(success=True, data={"output_path": output_path})

            return ToolResult(success=False, error=r.stderr[-800:])

        finally:
            for p in tmpfiles:
                try:
                    os.unlink(p)
                except OSError:
                    pass
