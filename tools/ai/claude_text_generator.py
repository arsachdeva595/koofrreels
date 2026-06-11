from __future__ import annotations

import json
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


class ClaudeTextGenerator(BaseTool):
    """Generate title and caption text for a reel from a creative brief."""

    name = "claude_text_generator"
    version = "0.1.0"
    tier = ToolTier.AI
    capability = "generate_text"
    provider = "anthropic"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "duration_seconds": {"type": "number"},
            "clip_names": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }

    def execute(self, params: dict) -> ToolResult:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return ToolResult(success=False, error="ANTHROPIC_API_KEY not set")

        prompt = params["prompt"]
        duration = params.get("duration_seconds", 30)
        clip_names = params.get("clip_names", [])
        clips_context = f"\nClip filenames: {', '.join(clip_names)}" if clip_names else ""

        system = (
            "You are a creative director writing concise, punchy text for Instagram Reels. "
            "Return ONLY valid JSON with fields: title (string, max 60 chars), caption (string or null, max 120 chars). "
            "Title goes at the top of the reel. Caption goes at the bottom. Keep it short, impactful, no hashtags."
        )
        user_msg = (
            f"Create title and caption for a {duration:.0f}-second Instagram Reel.\n"
            f"Creative brief: {prompt}{clips_context}\n\n"
            "Return JSON: {\"title\": \"...\", \"caption\": \"...\"}"
        )

        client = anthropic.Anthropic(api_key=api_key)
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text)
        except Exception as e:
            return ToolResult(success=False, error=f"Text generation failed: {e}")

        cost = (response.usage.input_tokens / 1_000_000 * 3.0) + (response.usage.output_tokens / 1_000_000 * 15.0)
        return ToolResult(success=True, data=result, cost_usd=cost)

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.getenv("ANTHROPIC_API_KEY") else ToolStatus.UNAVAILABLE
