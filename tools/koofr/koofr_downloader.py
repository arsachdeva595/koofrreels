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


class KoofrDownloader(BaseTool):
    name = "koofr_downloader"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "download_koofr"
    provider = "koofr"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "required": ["mount_id", "file_path", "dest_dir"],
        "properties": {
            "mount_id": {"type": "string"},
            "file_path": {"type": "string"},
            "dest_dir": {"type": "string"},
            "filename": {"type": "string"},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        import base64
        base_url = os.getenv("KOOFR_BASE_URL", "https://app.koofr.net/api/v2")
        email = os.getenv("KOOFR_EMAIL", "")
        app_password = os.getenv("KOOFR_APP_PASSWORD", "")
        if not email or not app_password:
            return ToolResult(success=False, error="Koofr credentials not set. Add KOOFR_EMAIL and KOOFR_APP_PASSWORD in settings.")

        auth_token = base64.b64encode(f"{email}:{app_password}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_token}"}

        mount_id = params["mount_id"]
        file_path = params["file_path"]
        dest_dir = Path(params["dest_dir"])
        dest_dir.mkdir(parents=True, exist_ok=True)

        filename = params.get("filename") or Path(file_path).name
        dest_path = dest_dir / filename

        if dest_path.exists():
            return ToolResult(
                success=True,
                data={"local_path": str(dest_path), "cached": True},
            )
        url = f"{base_url}/mounts/{mount_id}/files/get"

        try:
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers, params={"path": file_path}) as resp:
                    resp.raise_for_status()
                    with dest_path.open("wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 64):
                            fh.write(chunk)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=True,
            data={"local_path": str(dest_path), "cached": False, "size_bytes": dest_path.stat().st_size},
        )

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if (os.getenv("KOOFR_EMAIL") and os.getenv("KOOFR_APP_PASSWORD")) else ToolStatus.UNAVAILABLE
