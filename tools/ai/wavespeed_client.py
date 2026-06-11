"""
WaveSpeed API client — image-to-video generation.

Pattern:
  POST  https://api.wavespeed.ai/api/v3/{model_slug}   → {data: {id}}
  GET   …/predictions/{id}/result                       → poll until completed
  Download video from data.outputs[0].
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

WAVESPEED_BASE = "https://api.wavespeed.ai/api/v3"
POLL_INTERVAL = 5
MAX_WAIT = 600


class WaveSpeedClient(BaseTool):
    """WaveSpeed image-to-video client (Kling 3.0, WAN, etc.)."""

    name = "wavespeed_client"
    version = "0.1.0"
    tier = ToolTier.AI
    capability = "ai_video_generation"
    provider = "wavespeed"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    def execute(self, params: dict) -> ToolResult:
        operation = params.get("operation", "image_to_video")
        if operation == "image_to_video":
            return self._image_to_video(params)
        return ToolResult(success=False, error=f"Unknown wavespeed operation: {operation!r}")

    def _api_key(self) -> str:
        return os.getenv("WAVESPEED_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _image_to_video(self, params: dict) -> ToolResult:
        """
        params:
          model_slug   str   e.g. "wavespeed-ai/wan-i2v-480p"
          image_url    str   data URI or https URL
          prompt       str
          dest_path    str   local path to save .mp4
        """
        api_key = self._api_key()
        if not api_key:
            return ToolResult(success=False, error="WAVESPEED_API_KEY not set")

        model_slug = params.get("model_slug", "wavespeed-ai/wan-i2v-480p")
        image_url = params["image_url"]
        prompt = params.get("prompt", "")
        dest_path = params["dest_path"]

        submit_url = f"{WAVESPEED_BASE}/{model_slug}"
        payload = {"image": image_url, "prompt": prompt}

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(submit_url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                job_id = resp.json()["data"]["id"]
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        poll_url = f"{WAVESPEED_BASE}/predictions/{job_id}/result"
        deadline = time.time() + MAX_WAIT

        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.get(poll_url, headers=self._headers())
                    r.raise_for_status()
                    data = r.json().get("data", {})
                    status = data.get("status", "")

                    if status == "completed":
                        outputs = data.get("outputs") or []
                        video_url = outputs[0] if outputs else None
                        if not video_url:
                            return ToolResult(success=False, error=f"No output URL: {data}")

                        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                        with client.stream("GET", video_url, follow_redirects=True) as vr:
                            vr.raise_for_status()
                            with open(dest_path, "wb") as fh:
                                for chunk in vr.iter_bytes(65536):
                                    fh.write(chunk)

                        return ToolResult(success=True, data={"local_path": dest_path, "video_url": video_url})

                    if status == "failed":
                        return ToolResult(success=False, error=f"WaveSpeed job failed: {data}")

            except Exception as exc:
                return ToolResult(success=False, error=str(exc))

        return ToolResult(success=False, error="WaveSpeed polling timed out after 10 minutes")
