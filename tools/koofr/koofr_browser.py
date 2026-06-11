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

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _koofr_auth_headers() -> dict | None:
    """Return Basic Auth headers using email + application password."""
    import base64
    email = os.getenv("KOOFR_EMAIL", "")
    app_password = os.getenv("KOOFR_APP_PASSWORD", "")
    if not email or not app_password:
        return None
    token = base64.b64encode(f"{email}:{app_password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class KoofrBrowser(BaseTool):
    name = "koofr_browser"
    version = "0.2.0"
    tier = ToolTier.SOURCE
    capability = "list_koofr"
    provider = "koofr"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list_folders", "list_clips"],
            },
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
        },
        "required": ["operation"],
    }

    def execute(self, params: dict) -> ToolResult:
        base_url = os.getenv("KOOFR_BASE_URL", "https://app.koofr.net/api/v2")
        headers = _koofr_auth_headers()
        if headers is None:
            return ToolResult(success=False, error="Koofr credentials not set. Add KOOFR_EMAIL and KOOFR_APP_PASSWORD in settings.")
        op = params.get("operation")

        try:
            with httpx.Client(timeout=30) as client:
                if op == "list_folders":
                    return self._list_folders(client, base_url, headers)
                elif op == "list_clips":
                    path = params.get("path", "/")
                    recursive = params.get("recursive", False)
                    return self._list_clips(client, base_url, headers, path, recursive=recursive)
                else:
                    return ToolResult(success=False, error=f"Unknown operation: {op}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _list_folders(self, client: httpx.Client, base_url: str, headers: dict) -> ToolResult:
        resp = client.get(f"{base_url}/mounts", headers=headers)
        resp.raise_for_status()
        mounts = resp.json().get("mounts", [])
        folders = [
            {"id": m["id"], "name": m["name"], "path": "/"}
            for m in mounts
        ]
        return ToolResult(success=True, data={"folders": folders})

    def _get_mount_id(self, client: httpx.Client, base_url: str, headers: dict) -> str:
        resp = client.get(f"{base_url}/mounts", headers=headers)
        resp.raise_for_status()
        mounts = resp.json().get("mounts", [])
        if not mounts:
            raise RuntimeError("No Koofr mounts found")
        return mounts[0]["id"]

    def _list_clips(
        self, client: httpx.Client, base_url: str, headers: dict, path: str, recursive: bool = False
    ) -> ToolResult:
        mount_id = self._get_mount_id(client, base_url, headers)

        if recursive:
            all_clips: list[dict] = []
            self._walk(client, base_url, headers, mount_id, path, all_clips, depth=0)
            return ToolResult(
                success=True,
                data={"clips": all_clips, "subfolders": [], "mount_id": mount_id},
            )

        clips, subfolders = self._read_folder(client, base_url, headers, mount_id, path)
        return ToolResult(
            success=True,
            data={"clips": clips, "subfolders": subfolders, "mount_id": mount_id},
        )

    def _read_folder(
        self,
        client: httpx.Client,
        base_url: str,
        headers: dict,
        mount_id: str,
        path: str,
    ) -> tuple[list[dict], list[dict]]:
        resp = client.get(
            f"{base_url}/mounts/{mount_id}/files/list",
            headers=headers,
            params={"path": path},
        )
        resp.raise_for_status()
        files = resp.json().get("files", [])

        base = path.rstrip("/")
        clips = []
        subfolders = []
        for f in files:
            name = f["name"]
            fpath = f"{base}/{name}"
            if f.get("type") == "file":
                if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                    clips.append({
                        "file_id": f.get("id") or name,
                        "name": name,
                        "size_bytes": f.get("size", 0),
                        "modified": f.get("modified"),
                        "content_type": f.get("contentType", ""),
                        "path": fpath,
                    })
            elif f.get("type") == "dir":
                subfolders.append({"name": name, "path": fpath})

        return clips, subfolders

    def _walk(
        self,
        client: httpx.Client,
        base_url: str,
        headers: dict,
        mount_id: str,
        path: str,
        collector: list[dict],
        depth: int,
        max_depth: int = 5,
    ) -> None:
        if depth > max_depth:
            return
        try:
            clips, subfolders = self._read_folder(client, base_url, headers, mount_id, path)
            collector.extend(clips)
            for sf in subfolders:
                self._walk(client, base_url, headers, mount_id, sf["path"], collector, depth + 1, max_depth)
        except Exception:
            pass  # skip inaccessible folders silently

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if (os.getenv("KOOFR_EMAIL") and os.getenv("KOOFR_APP_PASSWORD")) else ToolStatus.UNAVAILABLE
