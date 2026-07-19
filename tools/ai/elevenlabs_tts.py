from __future__ import annotations

import os
from pathlib import Path

import httpx

from tools.ai.hinglish_pronunciation import apply_pronunciation_fixes
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class ElevenLabsTTS(BaseTool):
    name = "elevenlabs_tts"
    version = "0.1.0"
    tier = ToolTier.PROCESS
    capability = "text_to_speech"
    provider = "elevenlabs"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "required": ["text", "output_path"],
        "properties": {
            "text": {"type": "string"},
            "output_path": {"type": "string"},
            "voice_id": {"type": "string"},
            "model_id": {"type": "string"},
        },
    }

    DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel — clear, neutral English

    def execute(self, params: dict) -> ToolResult:
        api_key = os.getenv("ELEVENLABS_API_KEY", "")
        if not api_key:
            return ToolResult(success=False, error="ELEVENLABS_API_KEY not set in settings")

        text = params["text"].strip()
        if not text:
            return ToolResult(success=False, error="No text provided for TTS")
        text = apply_pronunciation_fixes(text)

        voice_id = params.get("voice_id") or os.getenv("ELEVENLABS_VOICE_ID", self.DEFAULT_VOICE)
        model_id = params.get("model_id", "eleven_multilingual_v2")
        output_path = Path(params["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": text,
            "model_id": model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                output_path.write_bytes(resp.content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        size = output_path.stat().st_size
        if size == 0:
            return ToolResult(success=False, error="ElevenLabs returned empty audio")

        return ToolResult(success=True, data={"output_path": str(output_path), "size_bytes": size})
