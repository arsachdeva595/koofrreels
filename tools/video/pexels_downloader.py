from __future__ import annotations

import os
from pathlib import Path

import httpx

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

PEXELS_BASE = "https://api.pexels.com"


class PexelsDownloader(BaseTool):
    """Search Pexels for videos matching a query and download the best match."""

    name = "pexels_downloader"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "gap_fill_video"
    provider = "pexels"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "required": ["query", "dest_dir"],
        "properties": {
            "query": {"type": "string"},
            "dest_dir": {"type": "string"},
            "orientation": {
                "type": "string",
                "enum": ["portrait", "landscape", "square"],
                "default": "portrait",
            },
            "min_duration": {"type": "integer", "default": 5},
            "max_results": {"type": "integer", "default": 3},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        api_key = os.getenv("PEXELS_API_KEY", "")
        if not api_key:
            return ToolResult(success=False, error="PEXELS_API_KEY not set")

        query = params["query"]
        dest_dir = Path(params["dest_dir"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        orientation = params.get("orientation", "portrait")
        min_dur = params.get("min_duration", 5)
        max_results = params.get("max_results", 3)

        headers = {"Authorization": api_key}

        try:
            with httpx.Client(timeout=30, base_url=PEXELS_BASE) as client:
                resp = client.get(
                    "/videos/search",
                    headers=headers,
                    params={
                        "query": query,
                        "orientation": orientation,
                        "per_page": 15,
                    },
                )
                resp.raise_for_status()
                videos = resp.json().get("videos", [])

            # Filter by duration and pick best
            candidates = [v for v in videos if v.get("duration", 0) >= min_dur]
            if not candidates:
                candidates = videos[:max_results]

            downloaded = []
            for video in candidates[:max_results]:
                # Prefer HD file
                files = sorted(
                    video.get("video_files", []),
                    key=lambda f: f.get("width", 0),
                    reverse=True,
                )
                video_file = next(
                    (f for f in files if f.get("file_type") == "video/mp4"),
                    files[0] if files else None,
                )
                if not video_file:
                    continue

                url = video_file["link"]
                filename = f"pexels_{video['id']}.mp4"
                dest = dest_dir / filename

                if not dest.exists():
                    with httpx.Client(timeout=120) as dl_client:
                        with dl_client.stream("GET", url) as r:
                            r.raise_for_status()
                            with dest.open("wb") as fh:
                                for chunk in r.iter_bytes(1024 * 64):
                                    fh.write(chunk)

                downloaded.append({
                    "local_path": str(dest),
                    "pexels_id": video["id"],
                    "duration_seconds": video.get("duration", 0),
                    "width": video_file.get("width"),
                    "height": video_file.get("height"),
                    "source": "pexels",
                    "selection_reason": f"Pexels gap-fill for: {query}",
                })

        except httpx.HTTPError as e:
            return ToolResult(success=False, error=str(e))

        if not downloaded:
            return ToolResult(success=False, error=f"No Pexels videos found for: {query}")

        return ToolResult(success=True, data={"clips": downloaded})

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.getenv("PEXELS_API_KEY") else ToolStatus.UNAVAILABLE
