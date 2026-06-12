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


class AudioMixer(BaseTool):
    """Mix voiceover + background music over a video, ducking music under voiceover."""

    name = "audio_mixer"
    version = "0.2.0"
    tier = ToolTier.PROCESS
    capability = "mix_audio"
    provider = "ffmpeg"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    input_schema = {
        "type": "object",
        "required": ["video_path", "output_path"],
        "properties": {
            "video_path": {"type": "string"},
            "output_path": {"type": "string"},
            "voiceover_path": {"type": ["string", "null"]},
            "music_path": {"type": ["string", "null"]},
            "duck_original": {"type": "boolean"},
            "original_volume": {"type": "number"},
            "music_volume": {"type": "number"},
            "voiceover_volume": {"type": "number"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        video_path = params["video_path"]
        output_path = params["output_path"]
        voiceover_path = params.get("voiceover_path")
        music_path = params.get("music_path")
        duck_original = params.get("duck_original", True)
        orig_vol = params.get("original_volume", 0.05 if duck_original else 1.0)
        music_vol = params.get("music_volume", 0.25)
        vo_vol = params.get("voiceover_volume", 1.0)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Get video duration
        dur = self._get_duration(video_path)

        has_video_audio = self._has_audio(video_path)

        inputs = ["-i", video_path]
        filter_parts = []
        mix_inputs = []
        input_idx = 1

        if has_video_audio:
            filter_parts.append(f"[0:a]volume={orig_vol}[orig]")
            mix_inputs.append("[orig]")

        if voiceover_path and Path(voiceover_path).exists():
            inputs += ["-i", voiceover_path]
            # apad=whole_dur ensures the VO stream spans the full video even if the
            # positioned file is shorter than the video (e.g. last scene VO ends early).
            filter_parts.append(
                f"[{input_idx}:a]volume={vo_vol},apad=whole_dur={dur}[vo]"
            )
            mix_inputs.append("[vo]")
            input_idx += 1

        if music_path and Path(music_path).exists():
            inputs += ["-i", music_path]
            filter_parts.append(
                f"[{input_idx}:a]aloop=loop=-1:size=2e+09,"
                f"atrim=duration={dur},volume={music_vol}[music]"
            )
            mix_inputs.append("[music]")
            input_idx += 1

        n_mix = len(mix_inputs)
        if n_mix == 0:
            # No audio sources at all — generate silence for the video duration
            filter_complex = f"anullsrc=r=44100:cl=stereo,atrim=duration={dur}[aout]"
        elif n_mix == 1:
            only = mix_inputs[0]
            filter_complex = ";".join(filter_parts) + f";{only}acopy[aout]"
        else:
            all_parts = ";".join(filter_parts)
            joined = "".join(mix_inputs)
            # normalize=0 keeps each stream at its set volume (no /n_inputs scaling)
            # duration=longest ensures audio runs for the full video even if one stream is short
            filter_complex = (
                f"{all_parts};"
                f"{joined}amix=inputs={n_mix}:duration=longest:normalize=0[aout]"
            )

        cmd = [
            "ffmpeg", "-nostdin", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", str(dur),
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr[-600:])

        return ToolResult(
            success=True,
            data={
                "output_path": output_path,
                "voiceover_added": voiceover_path is not None,
                "music_added": music_path is not None,
                "duration_seconds": dur,
            },
        )

    @staticmethod
    def _get_duration(path: str) -> float:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        try:
            return float(r.stdout.strip())
        except ValueError:
            return 30.0

    @staticmethod
    def _has_audio(path: str) -> bool:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        return bool(r.stdout.strip())
