from __future__ import annotations

import os

import anthropic

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class ClipScorer(BaseTool):
    name = "clip_scorer"
    version = "0.1.0"
    tier = ToolTier.AI
    capability = "score_clips"
    provider = "anthropic"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "required": ["prompt", "clips"],
        "properties": {
            "prompt": {"type": "string"},
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "clip_id": {"type": "string"},
                        "filename": {"type": "string"},
                        "duration_seconds": {"type": "number"},
                    },
                },
            },
        },
    }

    def execute(self, params: dict) -> ToolResult:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return ToolResult(success=False, error="ANTHROPIC_API_KEY not set")

        prompt = params["prompt"]
        clips = params["clips"]

        clip_list = "\n".join(
            f'- clip_id: {c["clip_id"]}, filename: {c.get("filename", "")}, duration: {c.get("duration_seconds", 0):.1f}s'
            for c in clips
        )

        system = (
            "You are a video editor scoring how well each clip matches a creative brief. "
            "Return a JSON array of objects with clip_id, score (0.0-1.0), and reason. "
            "Higher score = better match. Only return valid JSON, nothing else."
        )
        user_msg = (
            f"Creative brief: {prompt}\n\nClips:\n{clip_list}\n\n"
            "Score each clip from 0.0 to 1.0 based on how well the filename/context suggests it matches the brief. "
            "Return JSON array: [{\"clip_id\": ..., \"score\": ..., \"reason\": ...}]"
        )

        client = anthropic.Anthropic(api_key=api_key)
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text.strip()
            # Strip markdown code blocks if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            import json
            scores = json.loads(text)
        except Exception as e:
            return ToolResult(success=False, error=f"Claude scoring failed: {e}")

        cost = (response.usage.input_tokens / 1_000_000 * 3.0) + (response.usage.output_tokens / 1_000_000 * 15.0)
        return ToolResult(success=True, data={"scores": scores}, cost_usd=cost)

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.getenv("ANTHROPIC_API_KEY") else ToolStatus.UNAVAILABLE
