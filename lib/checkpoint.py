from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _project_dir(projects_root: str, project_id: str) -> Path:
    return Path(projects_root) / project_id


def write_checkpoint(
    projects_root: str,
    project_id: str,
    stage: str,
    status: str,
    artifacts: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> Path:
    project = _project_dir(projects_root, project_id)
    cp_dir = project / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    checkpoint = {
        "version": "1.0",
        "project_id": project_id,
        "stage": stage,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
        "metadata": metadata or {},
    }

    path = cp_dir / f"{stage}.json"
    path.write_text(json.dumps(checkpoint, indent=2))
    return path


def get_next_stage(
    projects_root: str,
    project_id: str,
    all_stages: list[str],
) -> str:
    cp_dir = _project_dir(projects_root, project_id) / "checkpoints"
    if not cp_dir.exists():
        return all_stages[0]

    completed = set()
    for stage in all_stages:
        cp = cp_dir / f"{stage}.json"
        if cp.exists():
            data = json.loads(cp.read_text())
            if data.get("status") in ("completed", "approved"):
                completed.add(stage)

    for stage in all_stages:
        if stage not in completed:
            return stage

    return all_stages[-1]


def load_artifact(projects_root: str, project_id: str, stage: str) -> dict | None:
    cp = _project_dir(projects_root, project_id) / "checkpoints" / f"{stage}.json"
    if not cp.exists():
        return None
    data = json.loads(cp.read_text())
    return data.get("artifacts", {})


def write_artifact(
    projects_root: str, project_id: str, name: str, artifact: dict
) -> Path:
    artifact_dir = _project_dir(projects_root, project_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{name}.json"
    path.write_text(json.dumps(artifact, indent=2))
    return path


def append_decision(
    projects_root: str,
    project_id: str,
    stage: str,
    decision: str,
    rationale: str,
    alternatives: list[str] | None = None,
) -> None:
    log_path = _project_dir(projects_root, project_id) / "artifacts" / "decision_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    if log_path.exists():
        entries = json.loads(log_path.read_text())

    entries.append({
        "stage": stage,
        "decision": decision,
        "rationale": rationale,
        "alternatives_considered": alternatives or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    log_path.write_text(json.dumps(entries, indent=2))
