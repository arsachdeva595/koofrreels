from __future__ import annotations

from pathlib import Path

import yaml


def load_pipeline(pipeline_defs_dir: str, pipeline_name: str) -> dict:
    path = Path(pipeline_defs_dir) / f"{pipeline_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Pipeline manifest not found: {path}")
    return yaml.safe_load(path.read_text())


def get_stage_order(pipeline: dict) -> list[str]:
    return [s["name"] for s in pipeline.get("stages", [])]


def get_stage(pipeline: dict, stage_name: str) -> dict | None:
    for stage in pipeline.get("stages", []):
        if stage["name"] == stage_name:
            return stage
    return None
