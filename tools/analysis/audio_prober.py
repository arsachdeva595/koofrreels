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


class AudioProber(BaseTool):
    name = "audio_prober"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "probe_audio"
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

        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "a", path,
        ]
        try:
            out = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            probe = json.loads(out.stdout) if out.returncode == 0 else {"streams": []}
        except Exception:
            probe = {"streams": []}

        audio_streams = probe.get("streams", [])
        has_audio = len(audio_streams) > 0

        volume_cmd = [
            "ffmpeg", "-i", path, "-af", "volumedetect", "-f", "null", "/dev/null",
        ]
        try:
            vol_out = subprocess.run(volume_cmd, capture_output=True, text=True, timeout=60)
            stderr = vol_out.stderr
            mean_volume = None
            max_volume = None
            for line in stderr.splitlines():
                if "mean_volume" in line:
                    mean_volume = float(line.split(":")[-1].strip().replace(" dB", ""))
                if "max_volume" in line:
                    max_volume = float(line.split(":")[-1].strip().replace(" dB", ""))
        except Exception:
            mean_volume = None
            max_volume = None

        issues = []
        if not has_audio:
            issues.append("No audio stream found")
        if mean_volume is not None and mean_volume < -50:
            issues.append(f"Audio very quiet: mean volume {mean_volume} dB")
        if max_volume is not None and max_volume > -0.1:
            issues.append(f"Audio may be clipping: max volume {max_volume} dB")

        return ToolResult(
            success=True,
            data={
                "has_audio": has_audio,
                "audio_stream_count": len(audio_streams),
                "mean_volume_db": mean_volume,
                "max_volume_db": max_volume,
                "issues": issues,
                "music_present": has_audio and (mean_volume is None or mean_volume > -50),
            },
        )
