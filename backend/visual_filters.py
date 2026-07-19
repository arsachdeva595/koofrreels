"""
Built-in visual filter prompts for AI Reels. Edit/add entries here — each is appended
into the still/video generation prompt as the "look" (grain/colour/lighting), never
literal on-screen text. Keys are stable ids referenced from the frontend and settings.
"""
from __future__ import annotations

from backend import settings_manager

BUILTIN_VISUAL_FILTERS: dict[str, str] = {
    "kodak_nostalgic": (
        "shot on 35mm film, authentic Kodak Portra 400 color grading, visible organic film grain, "
        "soft directional golden hour sunlight, subtle lens bloom, nostalgic and warm tones, cinematic pacing"
    ),
    "ultra_realistic": (
        "photorealistic 8k video, shot on Arri Alexa LF, hyper-detailed textures, true-to-life skin tones, "
        "natural ambient lighting, crisp focus, clear atmospheric depth, zero stylization, raw documentary footage"
    ),
    "moody_cinematic": (
        "cinematic masterpiece, dramatic low-key lighting, rich teal and orange color grading, deep shadows, "
        "intense volumetric directional light, high contrast, anamorphic lens flare, moody atmosphere"
    ),
    "clean_editorial": (
        "high-end commercial editorial look, shot on RED V-Raptor, professional studio multi-point lighting, "
        "soft diffused bright illumination, true color accuracy, minimal soft shadows, flawless catalog-grade production"
    ),
    "pastel_dreamy": (
        "dreamy ethereal atmosphere, soft pastel color palette, heavily diffused misty glow, glowing overexposed highlights, "
        "low contrast, gentle panning motion, fluid and airy visual style"
    ),
    "cyberpunk_neon": (
        "futuristic cyberpunk aesthetic, rain-slicked streets, vibrant neon glow reflections, "
        "shot on anamorphic lens, high contrast deep shadows, moody green and magenta color palette, "
        "slow tracking camera movement, ambient city hum and soft rain patter, no subtitles"
    ),
    "gritty_documentary": (
        "raw investigative documentary style, shot on a handheld camera with subtle natural shake, "
        "real-world ambient window light, desaturated natural tones, sharp textures, high realism, "
        "grounded perspective, muffled background noise, observational pacing"
    ),
    "macro_commercial": (
        "premium product macro commercial style, extreme close-up shot, shallow depth of field, "
        "intense surface texture detail, smooth slow-motion dolly-in, clean studio rim lighting, "
        "crisp micro-reflections, satisfying high-fidelity ASMR ambient sound design, no dialogue"
    ),
    "vintage_8mm": (
        "authentic 8mm home video footage, warm retro color grading, soft vintage lens vignette, "
        "light leaks and subtle gate flicker, low fidelity texture, handheld tracking motion, "
        "nostalgic analog hum and crackle audio, soft focus"
    ),
    "anime_cinematic": (
        "modern high-end cinematic anime visual style, beautiful hand-drawn aesthetic, dynamic light rays, "
        "vivid painterly color palette, sweeping dramatic camera movement, fluid cell-shaded motion physics, "
        "subtle ambient wind blowing sound, ethereal atmosphere"
    ),
    "pixar_animation": (
        "3D animated cinematic style, charming stylized character designs with large expressive eyes, "
        "intense subsurface scattering on skin and materials, vibrant warm lighting, rich color saturation, "
        "impeccable cloth and fur textures, fluid and elastic character physics, magical ambient room tone, "
        "shot on a virtual cinematic camera lens, sharp 3D render, no text overlays"
    ),
}


DEFAULT_VISUAL_FILTER = "kodak_nostalgic"
CUSTOM_VISUAL_FILTER_ID = "custom"


def resolve_visual_filter(filter_id: str) -> str:
    """Resolve a filter id (one of BUILTIN_VISUAL_FILTERS' keys, or "custom") to the
    descriptor text actually sent to the image/video model. Falls back to the default
    filter for an unknown id, or a blank custom prompt."""
    if filter_id == CUSTOM_VISUAL_FILTER_ID:
        custom = (settings_manager.get("custom_visual_filter_prompt", "") or "").strip()
        return custom or BUILTIN_VISUAL_FILTERS[DEFAULT_VISUAL_FILTER]
    return BUILTIN_VISUAL_FILTERS.get(filter_id, BUILTIN_VISUAL_FILTERS[DEFAULT_VISUAL_FILTER])
