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

PUBLER_BASE = "https://app.publer.io"


def _publer_headers() -> dict | tuple[None, str]:
    key = os.getenv("PUBLER_API_KEY", "")
    workspace = os.getenv("PUBLER_WORKSPACE_ID", "")
    if not key:
        return None, "PUBLER_API_KEY not set. Add it in Settings → API Keys."
    if not workspace:
        return None, "PUBLER_WORKSPACE_ID not set. Add it in Settings → API Keys (find it in your Publer URL: app.publer.io/workspaces/YOUR_ID)."
    return {
        "Authorization": f"Bearer-API {key}",
        "Publer-Workspace-Id": workspace,
        "Content-Type": "application/json",
    }, None


class PublerClient(BaseTool):
    name = "publer_client"
    version = "0.1.0"
    tier = ToolTier.DELIVER
    capability = "publer"
    provider = "publer"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list_accounts", "upload_media", "schedule_post"],
            },
            "file_path": {"type": "string"},
            "account_ids": {"type": "array", "items": {"type": "string"}},
            "caption": {"type": "string"},
            "schedule_at": {"type": "string"},
            "media_ids": {"type": "array", "items": {"type": "string"}},
        },
    }

    def execute(self, params: dict) -> ToolResult:
        headers, err = _publer_headers()
        if err:
            return ToolResult(success=False, error=err)

        op = params["operation"]
        try:
            with httpx.Client(timeout=60, base_url=PUBLER_BASE) as client:
                if op == "list_accounts":
                    return self._list_accounts(client, headers)
                elif op == "upload_media":
                    return self._upload_media(client, headers, params["file_path"])
                elif op == "schedule_post":
                    return self._schedule_post(client, headers, params)
                else:
                    return ToolResult(success=False, error=f"Unknown operation: {op}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _list_accounts(self, client: httpx.Client, headers: dict) -> ToolResult:
        resp = client.get("/api/v1/accounts", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        # Publer may return a list directly or {"accounts": [...]}
        raw = data if isinstance(data, list) else data.get("accounts", [])
        accounts = [
            {
                "id": str(a.get("id", "")),
                "name": a.get("name", ""),
                "platform": a.get("type", ""),
                "username": a.get("username", ""),
            }
            for a in raw
        ]
        return ToolResult(success=True, data={"accounts": accounts})

    def _upload_media(self, client: httpx.Client, headers: dict, file_path: str) -> ToolResult:
        p = Path(file_path)
        if not p.exists():
            return ToolResult(success=False, error=f"File not found: {file_path}")

        with p.open("rb") as f:
            resp = client.post(
                "/api/v1/media",
                headers=headers,
                files={"file": (p.name, f, "video/mp4")},
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        return ToolResult(
            success=True,
            data={"media_id": str(data.get("id", "")), "url": data.get("url", "")},
        )

    def _schedule_post(self, client: httpx.Client, headers: dict, params: dict) -> ToolResult:
        body = {
            "account_ids": params.get("account_ids", []),
            "text": params.get("caption", ""),
            "media_ids": params.get("media_ids", []),
        }
        if params.get("schedule_at"):
            body["scheduled_at"] = params["schedule_at"]

        resp = client.post("/api/v1/posts/schedule", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return ToolResult(
            success=True,
            data={"post_id": str(data.get("id", "")), "status": data.get("status", "")},
        )

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if (os.getenv("PUBLER_API_KEY") and os.getenv("PUBLER_WORKSPACE_ID")) else ToolStatus.UNAVAILABLE
