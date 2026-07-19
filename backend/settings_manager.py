"""
Persists all user-configurable settings to settings.json in the project root.
Also syncs sensitive keys into environment variables so tools can read them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"

_DEFAULTS: dict[str, Any] = {
    # API keys
    "koofr_email": "",
    "koofr_app_password": "",
    "anthropic_api_key": "",
    "publer_api_key": "",
    "publer_workspace_id": "",
    "pexels_api_key": "",
    "elevenlabs_api_key": "",
    "elevenlabs_voice_id": "",
    "fal_api_key": "",
    "wavespeed_api_key": "",
    # Google Vertex AI (AI Reels — Veo3)
    "vertex_project_id": "",
    "vertex_location": "us-central1",
    "google_credentials_path": "",
    # Standard Veo model for Story/Character/No-Characters/Product clips.
    "veo_standard_model": "veo-3.1-lite-generate-001",
    # Reference-capable Veo 3.1 model for UGC mode (creator + product in one shot).
    # Must be enabled in the project's Vertex Model Garden.
    "veo_reference_model": "veo-3.1-generate-001",
    # ── AI Reels — video model (applies to ALL AI reel types) ──────────────────
    # Which model turns a prompt/keyframe into a clip. "veo3" uses Google Vertex
    # (veo_standard_model + vertex_*); "fal"/"wavespeed" use the endpoints below and
    # take a first-frame image. Endpoints are entered manually.
    "ai_video_provider": "veo3",   # veo3 | fal | wavespeed
    "fal_video_endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
    "wavespeed_video_model": "bytedance/seedance-2.0/image-to-video",
    # ── Cinematic reel type ────────────────────────────────────────────────────
    # Keyframe stills provider + model. "fal" (Nano Banana etc.), "google" (Gemini
    # image = Nano Banana, on your Vertex — no fal markup), or "wavespeed".
    "cinematic_keyframe_provider": "fal",   # fal | google | wavespeed
    "cinematic_keyframe_endpoint": "fal-ai/nano-banana/edit",       # fal image endpoint
    "cinematic_keyframe_google_model": "gemini-2.5-flash-image",    # Vertex image model
    "cinematic_keyframe_wavespeed_model": "google/nano-banana-pro/edit",  # WaveSpeed image model
    # Character-lock strategy (C1): "sheet" = generate one character sheet first and
    # condition every still on it; "chain" = feed the prior still into the next;
    # "none" = condition only on the original reference.
    "cinematic_chaining_strategy": "sheet",
    # Target seconds per shot. Video bills per second at a 5s floor per clip, so fewer/
    # longer shots = fewer clips billed = less waste. ~5s ≈ uses the full clip.
    "cinematic_shot_seconds": 5.0,
    # Koofr
    "koofr_default_folder": "/",
    # Brand voice (markdown string)
    "brand_voice": "",
    # Publer
    "publer_enabled": False,
    "publer_default_account_ids": [],
    "publer_auto_schedule": False,
    "publer_schedule_time": "09:00",
    # Pexels gap-fill
    "pexels_gap_fill_enabled": False,
    # Queue
    "queue_auto_run": False,
    "queue_run_interval_hours": 24,
    # AI Reels — custom visual filter (used when "Custom" is selected in the app)
    "custom_visual_filter_name": "Custom",
    "custom_visual_filter_prompt": "",
    # Meme clips
    "meme_master_prompt": "",
    # Local output directory — where final mp4 files are copied after rendering
    "local_output_dir": "",
}

_ENV_MAP = {
    "koofr_email": "KOOFR_EMAIL",
    "koofr_app_password": "KOOFR_APP_PASSWORD",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "publer_api_key": "PUBLER_API_KEY",
    "publer_workspace_id": "PUBLER_WORKSPACE_ID",
    "pexels_api_key": "PEXELS_API_KEY",
    "elevenlabs_api_key": "ELEVENLABS_API_KEY",
    "elevenlabs_voice_id": "ELEVENLABS_VOICE_ID",
    "fal_api_key": "FAL_API_KEY",
    "wavespeed_api_key": "WAVESPEED_API_KEY",
    "google_credentials_path": "GOOGLE_APPLICATION_CREDENTIALS",
}


def load() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        merged = {**_DEFAULTS, **data}
        _sync_env(merged)
        return merged
    except json.JSONDecodeError:
        return dict(_DEFAULTS)


def save(updates: dict[str, Any]) -> dict[str, Any]:
    current = load()
    current.update({k: v for k, v in updates.items() if k in _DEFAULTS})
    SETTINGS_PATH.write_text(json.dumps(current, indent=2))
    _sync_env(current)
    return current


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def _sync_env(settings: dict[str, Any]) -> None:
    for setting_key, env_key in _ENV_MAP.items():
        value = settings.get(setting_key, "")
        if value:
            os.environ[env_key] = value


def get_brand_voice() -> str:
    return load().get("brand_voice", "")


def get_koofr_folder() -> str:
    return load().get("koofr_default_folder", "/")


def get_local_output_dir() -> str:
    return load().get("local_output_dir", "")
