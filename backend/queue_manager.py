"""
Reel idea queue. Ideas can be added manually and run automatically
on a configured interval (or triggered on demand).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUEUE_PATH = Path(__file__).parent.parent / "reel_queue.json"


def _load() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text())
    except json.JSONDecodeError:
        return []


def _save(items: list[dict]) -> None:
    QUEUE_PATH.write_text(json.dumps(items, indent=2))


def list_queue() -> list[dict]:
    return _load()


def add_idea(idea: dict[str, Any]) -> dict:
    items = _load()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "job_id": None,
        "brief": {
            "mode": idea.get("mode", "random"),
            "target_duration_seconds": idea.get("target_duration_seconds", 30),
            "koofr_folder": idea.get("koofr_folder"),
            "clip_ids": idea.get("clip_ids"),
            "prompt": idea.get("prompt"),
            "music_file": idea.get("music_file"),
            "include_text": idea.get("include_text", True),
            "text_hints": idea.get("text_hints"),
        },
        "publer_push": idea.get("publer_push", False),
        "publer_account_ids": idea.get("publer_account_ids", []),
        "publer_caption": idea.get("publer_caption", ""),
        "publer_schedule_at": idea.get("publer_schedule_at"),
        "notes": idea.get("notes", ""),
    }
    items.append(entry)
    _save(items)
    return entry


def update_idea(idea_id: str, updates: dict) -> dict | None:
    items = _load()
    for item in items:
        if item["id"] == idea_id:
            item.update(updates)
            _save(items)
            return item
    return None


def delete_idea(idea_id: str) -> bool:
    items = _load()
    new_items = [i for i in items if i["id"] != idea_id]
    if len(new_items) == len(items):
        return False
    _save(new_items)
    return True


def get_pending() -> list[dict]:
    return [i for i in _load() if i["status"] == "pending"]
