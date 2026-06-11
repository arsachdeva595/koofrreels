from __future__ import annotations

import os
from pathlib import Path


def get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


KOOFR_TOKEN = get("KOOFR_TOKEN")
KOOFR_BASE_URL = get("KOOFR_BASE_URL", "https://app.koofr.net/api/v2")
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY")
MUSIC_LIBRARY_PATH = Path(get("MUSIC_LIBRARY_PATH", "./music"))
PROJECTS_DIR = Path(get("PROJECTS_DIR", "./projects"))
UPLOADS_DIR = Path(get("UPLOADS_DIR", "./uploads"))
PIPELINE_DEFS_DIR = Path(get("PIPELINE_DEFS_DIR", "./pipeline_defs"))
SCHEMAS_DIR = Path(get("SCHEMAS_DIR", "./schemas"))
SKILLS_DIR = Path(get("SKILLS_DIR", "./skills"))
