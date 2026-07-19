"""
Preset "model" photos for AI Reels' Character / UGC / Cinematic reel types — pick one of
these instead of uploading your own reference photo every time. Unlike Studio's
MODEL_LIBRARY (text-description-only, no real photo), these presets carry an actual
photo path: Character/UGC/Cinematic condition generation on the real reference image
(vision description + image-conditioned Veo/fal/wavespeed calls), not just a text blurb.

Edit this file directly to add/replace presets — drop the photo under
frontend/assets/model_presets/ (served at /static/assets/model_presets/... via the
existing /static mount, and used as-is server-side as character_image_path) and add
an entry below. `voice_id` looks it up from VOICE_BY_GENDER by the preset's `gender`
unless a preset needs its own explicit override (add a "voice_id" key to do that).
"""
from __future__ import annotations

_PRESET_DIR = "frontend/assets/model_presets"

MODEL_PRESETS: list[dict] = [
    {"id": "preset_1", "name": "Sana", "gender": "female", "photo": f"{_PRESET_DIR}/sana.jpg"},
    {"id": "preset_2", "name": "Manav", "gender": "male", "photo": f"{_PRESET_DIR}/manav.jpg"},
    {"id": "preset_3", "name": "Prachee", "gender": "female", "photo": f"{_PRESET_DIR}/prachee.jpg"},
]

VOICE_BY_GENDER: dict[str, str] = {
    "female": "mg9npuuaf8WJphS6E0Rt",
    "male": "6MoEUz34rbRrmmyxgRm4",
}


def get_preset(preset_id: str) -> dict | None:
    return next((p for p in MODEL_PRESETS if p["id"] == preset_id), None)


def voice_for_preset(preset: dict) -> str | None:
    """The preset's own voice_id override if set, else the gender-mapped default."""
    return preset.get("voice_id") or VOICE_BY_GENDER.get(preset.get("gender", "")) or None
