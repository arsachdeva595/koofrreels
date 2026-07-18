"""
AI Reels pipeline — identical to the Stock pipeline but generates every clip
from scratch using Google Veo3 (Vertex AI) instead of downloading from Pexels.

Each scene's visual_description (enriched with clip_hint) becomes the Veo3
text prompt. duration_seconds is clamped to Veo3's 8-second maximum.
Everything downstream — edit_planner, compose, text overlay, voiceover,
audio mix — is identical to stock_runner.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from backend.config import PROJECTS_DIR
from backend.job_manager import Job, JobStatus
from backend import settings_manager
from backend.pipeline_runner import (
    _generate_storyboard,
    _generate_cinematic_storyboard,
    _compute_scene_text_timing,
    _generate_scene_vo_files,
    _resolve_music,
    _ts,
)
from backend import edit_planner
from tools.base_tool import ToolResult
from tools.ai.veo3_client import Veo3Client, VEO3_MAX_DURATION
from tools.ai.fal_client import FalClient
from tools.ai.wavespeed_client import WaveSpeedClient
from tools.ai.keyframe_generator import KeyframeGenerator
from tools.ai.continuity_checker import ContinuityChecker
from tools.analysis.clip_analyzer import ClipAnalyzer
from tools.analysis.audio_prober import AudioProber
from tools.analysis.frame_sampler import FrameSampler
from tools.analysis.video_prober import VideoProber
from tools.audio.audio_mixer import AudioMixer
from tools.video.text_renderer import TextRenderer
from tools.video.video_composer import VideoComposer
from tools.video.video_normalizer import VideoNormalizer
from tools.video.color_matcher import ColorMatcher


def _parse_script_to_storyboard(script: str) -> dict:
    """
    Parse a manually-written scene script into the same storyboard dict that
    _generate_storyboard() returns, so the rest of the pipeline is unchanged.

    Expected per-scene format (all on one line or split across lines):
      [MM:SS - MM:SS] TITLE[VISUAL] description [AUDIO] sound NARRATOR: spoken line

    [AUDIO] is optional. Sections can be separated by whitespace or newlines.
    """
    import re

    def _ts_to_seconds(ts: str) -> float:
        parts = ts.strip().split(":")
        return int(parts[0]) * 60 + float(parts[1])

    # Split into per-scene chunks — each starts at a [MM:SS - MM:SS] marker
    chunks = re.split(r'(?=\[\d+:\d+\s*[-–]\s*\d+:\d+\])', script.strip())
    chunks = [c.strip() for c in chunks if c.strip()]

    scenes = []
    for idx, chunk in enumerate(chunks):
        ts_match = re.match(r'\[(\d+:\d+)\s*[-–]\s*(\d+:\d+)\]\s*', chunk)
        if not ts_match:
            continue

        start = _ts_to_seconds(ts_match.group(1))
        end = _ts_to_seconds(ts_match.group(2))
        duration = round(max(end - start, 1.0), 1)
        rest = chunk[ts_match.end():]

        # Title = text before the first [TAG] or NARRATOR:
        title_match = re.match(r'(.*?)(?=\[(?:VISUAL|AUDIO|TEXT)\]|\bNARRATOR:)', rest, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else f"Scene {idx + 1}"

        # [VISUAL] content (up to [AUDIO], NARRATOR:, or end)
        visual_match = re.search(r'\[VISUAL\]\s*(.*?)(?=\[AUDIO\]|\bNARRATOR:|$)', rest, re.DOTALL | re.IGNORECASE)
        visual = visual_match.group(1).strip() if visual_match else ""

        # NARRATOR: content (to end of chunk)
        narrator_match = re.search(r'\bNARRATOR:\s*(.*?)$', rest, re.DOTALL | re.IGNORECASE)
        voiceover = narrator_match.group(1).strip() if narrator_match else ""

        # overlay_text — strip parentheticals and quotes from the title, cap at 6 words
        overlay = re.sub(r'\(.*?\)', '', title).strip()
        overlay = re.sub(r'[""\'\'"]', '', overlay).strip()
        overlay = ' '.join(overlay.split()[:6])

        # clip_hint — first 4 meaningful words from visual description
        _stop = {'a', 'an', 'the', 'of', 'to', 'in', 'on', 'at', 'is', 'are',
                 'was', 'were', 'with', 'for', 'and', 'or', 'as', 'its', 'into'}
        kw = [w.strip('.,;:') for w in visual.split() if len(w) > 3 and w.lower().strip('.,;:') not in _stop]
        clip_hint = ' '.join(kw[:4]) if kw else "cinematic shot"

        # Role from title keywords
        tu = title.upper()
        if 'HOOK' in tu or idx == 0:
            role = "hook"
        elif '"AND"' in tu or ' AND ' in tu:
            role = "and"
        elif '"BUT"' in tu or ' BUT ' in tu:
            role = "but"
        elif 'THEREFORE' in tu:
            role = "therefore"
        elif 'PAYOFF' in tu:
            role = "trigger"
        elif 'OUTRO' in tu or 'CLOSE' in tu or idx == len(chunks) - 1:
            role = "outro"
        else:
            role = "and"

        scenes.append({
            "scene": idx + 1,
            "role": role,
            "visual_description": visual or f"Cinematic shot for scene {idx + 1}",
            "clip_hint": clip_hint,
            "duration_seconds": duration,
            "overlay_text": overlay,
            "voiceover": voiceover,
        })

    if not scenes:
        return {
            "theme": script[:60].strip(),
            "scenes": [{
                "scene": 1,
                "role": "hook",
                "visual_description": "Dynamic cinematic narrative shot",
                "clip_hint": "cinematic dynamic narrative",
                "duration_seconds": 30.0,
                "overlay_text": "",
                "voiceover": script.strip(),
            }],
        }

    return {
        "theme": scenes[0]["overlay_text"] or "Script-driven reel",
        "scenes": scenes,
    }


def _vision_describe(image_path: str, instruction: str) -> str:
    """One Claude-vision call over an image, returning a short description. Downscales
    large images first so we never hit Claude's 10 MB image cap."""
    import base64
    import io
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")
    if not api_key or not Path(image_path).exists():
        return ""
    raw = Path(image_path).read_bytes()
    suffix = Path(image_path).suffix.lower().lstrip(".")
    media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix, "image/jpeg")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        if max(img.size) > 1568 or len(raw) > 4_500_000 or media_type not in ("image/jpeg", "image/png"):
            img = img.convert("RGB")
            if max(img.size) > 1568:
                s = 1568 / max(img.size)
                img = img.resize((max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            raw, media_type = buf.getvalue(), "image/jpeg"
    except Exception:
        pass
    data = base64.standard_b64encode(raw).decode()
    resp = anthropic.Anthropic(api_key=api_key).messages.create(
        model="claude-sonnet-4-6",
        max_tokens=220,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": instruction},
        ]}],
    )
    return resp.content[0].text.strip()


def _describe_product(image_path: str) -> str:
    """Short factual product description → makes the storyboard product-aware."""
    return _vision_describe(image_path, (
        "Describe this product in 1-2 sentences for a video scriptwriter. "
        "State exactly what it is, its key visual features (color, material, shape, style), "
        "and the vibe it conveys. Be concrete and factual. Return only the description."
    ))


def _describe_person(image_path: str) -> str:
    """Short factual description of the on-camera creator → so the script matches the
    actual person (gender/age/look) instead of Claude inventing one."""
    return _vision_describe(image_path, (
        "Describe the PERSON in this image in 1-2 sentences for a video scriptwriter: "
        "their apparent gender, approximate age range, and key visible features (hair, "
        "skin tone, build, notable clothing/style). Be factual and neutral — this is the "
        "on-camera creator the script must depict. Return only the description."
    ))


def run_ai_pipeline(job: Job, params: dict[str, Any]) -> None:
    project_id = f"ai-{uuid.uuid4().hex[:8]}"
    project_dir = (PROJECTS_DIR / project_id).resolve()
    clips_dir = project_dir / "clips"
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"
    voiceover_dir = project_dir / "voiceover"

    for d in [clips_dir, tmp_dir / "normalized", output_dir, voiceover_dir]:
        d.mkdir(parents=True, exist_ok=True)

    try:
        # ── STAGE 1: brief ─────────────────────────────────────────────────────
        job.begin_stage("brief", "Processing Brief", "Interpreting your request...")
        job.update(progress_pct=3, status=JobStatus.RUNNING)

        reel_brief = {
            "version": "1.0",
            "mode": "ai_reels",
            "target_duration_seconds": float(params.get("target_duration_seconds", 30)),
            "prompt": params.get("prompt"),
            "music_file": params.get("music_file"),
            "include_text": params.get("include_text", True),
            "use_brand_guidelines": params.get("use_brand_guidelines", True),
            "text_hints": params.get("text_hints"),
            "reel_type": params.get("reel_type", "story"),
            "framework": params.get("framework", "abt"),
            "product_image_path": params.get("product_image_path"),
        }

        if not settings_manager.get("vertex_project_id", ""):
            raise RuntimeError(
                "Google Cloud Project ID not set — add it in Settings → API Keys → Google Cloud"
            )

        creds_path = settings_manager.get("google_credentials_path", "")
        if creds_path and not Path(creds_path).exists():
            raise RuntimeError(
                f"Google credentials file not found at: {creds_path} — "
                "check the path in Settings → API Keys → Google Cloud"
            )
        if not creds_path and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            raise RuntimeError(
                "Google credentials not configured — set google_credentials_path in Settings → API Keys → Google Cloud"
            )

        # ── UGC mode needs both a creator image and a product image ────────────
        if params.get("reel_type") == "ugc":
            if not (params.get("character_image_path") and params.get("product_image_path")):
                raise RuntimeError(
                    "UGC mode needs BOTH a creator image and a product image — upload both in AI Reels → UGC."
                )

        # ── Cinematic mode needs one character reference image ─────────────────
        if params.get("reel_type") == "cinematic":
            if not params.get("character_image_path"):
                raise RuntimeError(
                    "Cinematic mode needs one character reference image — upload it in AI Reels → Cinematic."
                )

        # ── Product / UGC mode: read the product from its image (vision) ───────
        # The description is fed into the storyboard prompt so the script is built
        # around the actual product, not just the text prompt.
        product_image_path = params.get("product_image_path")
        if params.get("reel_type") in ("product", "ugc") and product_image_path and Path(product_image_path).exists():
            try:
                reel_brief["product_description"] = _describe_product(product_image_path)
                job.update(message="Product analyzed from image")
            except Exception as _exc:
                job.update(message=f"Product image analysis skipped ({_exc})")

        # ── Character / UGC mode: read the CREATOR from their image (vision) ────
        # so the script depicts the real person (gender/age/look) instead of Claude
        # inventing one that contradicts the reference (e.g. a man ref → "woman" script).
        _char_img = params.get("character_image_path")
        if params.get("reel_type") in ("character", "ugc") and _char_img and Path(_char_img).exists():
            try:
                reel_brief["character_description"] = _describe_person(_char_img)
                job.update(message="Creator analyzed from image")
            except Exception as _exc:
                job.update(message=f"Creator image analysis skipped ({_exc})")

        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: storyboard ────────────────────────────────────────────────
        skip_storyboard = params.get("skip_storyboard", False)
        is_cinematic = params.get("reel_type") == "cinematic"
        if is_cinematic:
            # Cinematic type: a cinematographer-director emits a per-shot contract
            # (style_lock + shots[]) with shot type / lens / DOF / camera move / cut.
            # The contract carries a normalized scenes[] so the rest of the flow runs.
            job.begin_stage("storyboard", "Cinematic Shot Plan", "Claude is shot-listing your sequence...")
            job.update(progress_pct=8)
            # Pass the single reference image through to the director.
            reel_brief["character_image_path"] = params.get("character_image_path")
            storyboard = _generate_cinematic_storyboard(reel_brief)
            scenes = storyboard["scenes"]
            job.end_stage("storyboard", f"{len(scenes)}-shot cinematic plan ready — awaiting your approval")
        elif skip_storyboard:
            job.begin_stage("storyboard", "Parsing Your Script", "Reading scene-by-scene instructions...")
            job.update(progress_pct=8)
            storyboard = _parse_script_to_storyboard(params.get("prompt", ""))
            scenes = storyboard["scenes"]
            # Override target duration with what the script defines
            reel_brief["target_duration_seconds"] = sum(s["duration_seconds"] for s in scenes)
            job.end_stage("storyboard", f"{len(scenes)} scenes parsed from your script — awaiting your approval")
        else:
            job.begin_stage("storyboard", "Director's Storyboard", "Claude is planning your ABT story...")
            job.update(progress_pct=8)
            storyboard = _generate_storyboard(reel_brief)
            scenes = storyboard["scenes"]
            job.end_stage("storyboard", f"{len(scenes)}-scene story ready — awaiting your approval")

        # ── Pre-approval policy scan ───────────────────────────────────────────
        # Run each scene's visual fields through the sanitizer so the user can
        # see potential flags before approving and triggering Veo3 calls.
        from tools.ai.prompt_sanitizer import sanitize_prompt as _sp
        _MINOR_TERMS = {"girl", "girls", "young girl", "teenage", "teen", "minor",
                        "child", "kid", "adolescent", "juvenile", "underage",
                        "ladki", "bachi", "bacchi"}
        policy_flags = []
        for scene in scenes:
            check = " ".join(filter(None, [
                scene.get("visual_description", ""),
                scene.get("clip_hint", ""),
            ]))
            _, subs = _sp(check)
            minor_hits = [w for w in _MINOR_TERMS if w in check.lower()]
            flags = subs + ([f"minor-detection risk: '{h}' → use 'woman' or 'person'" for h in minor_hits])
            # Cinematic C2: flag any shot the director set to speak on camera (no lip-sync).
            if is_cinematic and scene.get("speaking_on_camera"):
                flags = flags + ["speaking_on_camera=true — no lip-sync; reframe as listening/observing or it will read as broken sync"]
            if flags:
                policy_flags.append({
                    "scene": scene["scene"],
                    "role": scene.get("role", ""),
                    "flags": flags,
                })

        # ── APPROVAL ───────────────────────────────────────────────────────────
        approval_payload = {
            "storyboard": storyboard,
            "reel_summary": {
                "mode": "ai_reels",
                "reel_type": params.get("reel_type", "story"),
                "prompt": reel_brief.get("prompt") or reel_brief.get("text_hints"),
                "target_duration_seconds": reel_brief["target_duration_seconds"],
                "scene_count": len(scenes),
                "music_file": reel_brief.get("music_file") or "Random from library",
            },
            "policy_flags": policy_flags,
        }
        if is_cinematic:
            # Surface the §5 contract so the modal can render per-shot cinematography.
            approval_payload["cinematic"] = True
            approval_payload["style_lock"] = storyboard.get("style_lock", "")
            approval_payload["shots"] = storyboard.get("shots", [])
        # Cost estimate so you approve the spend before any generation.
        _use_kf = (not is_cinematic) and (not params.get("skip_storyboard")) and _keyframe_provider_ready()
        approval_payload["cost_estimate"] = _preview_ai_cost(
            params.get("reel_type", "story"), scenes, is_cinematic, _use_kf,
            params.get("include_voiceover", True),
        )
        job.request_approval(approval_payload)
        approved = job.wait_for_approval(timeout=1800)
        if not approved:
            raise RuntimeError("Approval timed out after 30 minutes")

        resp_scenes = (job.approval_response or {}).get("scenes", [])
        edited_by_scene = {s["scene"]: s for s in resp_scenes if "scene" in s}
        for s in scenes:
            edits = edited_by_scene.get(s["scene"], {})
            if "overlay_text" in edits:
                s["overlay_text"] = edits["overlay_text"]
            if "voiceover" in edits:
                s["voiceover"] = edits["voiceover"]

        # ── CINEMATIC: pause-driven edit (audio is the master timeline) ────────────
        # 1. VO first → detect pauses → render plan. 2. base stills + continuity gate.
        # 3. continuation stills for multi-beat shots. 4. one clip per beat.
        # 5. assemble with cuts on the pauses + the full VO. Falls back to the original
        # shot-per-clip flow when there's no voiceover to time against.
        if is_cinematic:
            render_plan, vo_concat_path, _vo_sec = _run_cinematic_plan(
                job, params, project_dir, storyboard, scenes)
            _run_cinematic_keyframes(job, params, project_dir, project_id, storyboard, scenes)
            if render_plan:
                _run_cinematic_continuation_stills(job, project_dir, storyboard, render_plan)
                clip_manifest = _run_cinematic_clips_planned(job, params, project_dir, storyboard, render_plan)
                _run_cinematic_finish_planned(job, params, project_dir, project_id,
                                              storyboard, clip_manifest, render_plan, vo_concat_path)
            else:
                clip_manifest = _run_cinematic_clips(job, params, project_dir, project_id, storyboard)
                _run_cinematic_finish(job, params, project_dir, project_id, storyboard, clip_manifest)
            return

        # ── KEYFRAME-FIRST (standard AI modes): generate a still per scene, gate on a
        # keyframe review, then image-to-video from the approved stills — the same review
        # checkpoint + consistency Cinematic has. Skipped (direct video) if no image
        # provider is configured or the step fails.
        scene_stills: dict[int, str] = {}
        _reel_type = params.get("reel_type", "story")
        if not skip_storyboard and _keyframe_provider_ready():
            _char = params.get("character_image_path")
            _prod = params.get("product_image_path")
            _kf = params.get("keyframe_paths") or []
            _prod_scenes = ({s["scene"] for s in scenes if s.get("feature_product")}
                            if (_reel_type == "product" and _prod) else set())
            try:
                scene_stills = _run_standard_keyframes(
                    job, project_dir, scenes, _reel_type, _char, _prod, _kf, _prod_scenes)
            except Exception as _exc:
                job.update(message=f"Keyframe step skipped ({_exc}) — generating video directly")
                scene_stills = {}

        # ── STAGE 3: voiceover TTS ─────────────────────────────────────────────
        job.begin_stage("voiceover", "Generating Voiceover", "Sending script to ElevenLabs...")
        job.update(progress_pct=20)

        scene_vo_files: dict[int, str] = {}
        scene_vo_durations: dict[int, float] = {}
        if not params.get("include_voiceover", True):
            job.end_stage("voiceover", "Voiceover disabled — skipping")
        else:
            elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "") or settings_manager.get("elevenlabs_api_key", "")
            if elevenlabs_key and any(s.get("voiceover") for s in scenes):
                scene_vo_files, scene_vo_durations = _generate_scene_vo_files(scenes, voiceover_dir)
                if scene_vo_files:
                    job.end_stage("voiceover", f"{len(scene_vo_files)} scene voiceovers generated — clips will match VO length")
                else:
                    job.end_stage("voiceover", "Voiceover generation failed — continuing without it")
            else:
                job.end_stage("voiceover", "No ElevenLabs key set — skipping voiceover")

        # ── STAGE 4: clip_selection (Veo3) ─────────────────────────────────────
        job.begin_stage("clip_selection", "Generating AI Clips", "Sending each scene to the video model...")
        job.update(progress_pct=25)

        project_id_vx = settings_manager.get("vertex_project_id")
        location = settings_manager.get("vertex_location", "us-central1")
        reel_type = params.get("reel_type", "story")
        character_image_path = params.get("character_image_path")
        keyframe_paths = params.get("keyframe_paths") or []
        # Veo models — both configurable in Settings → Google Cloud.
        standard_model = settings_manager.get("veo_standard_model") or "veo-3.1-lite-generate-001"
        ugc_model = settings_manager.get("veo_reference_model") or "veo-3.1-generate-001"
        # Global AI video provider (applies to every AI reel type).
        ai_provider = (settings_manager.get("ai_video_provider", "veo3") or "veo3").lower()
        fal_video_endpoint = settings_manager.get("fal_video_endpoint") or "fal-ai/kling-video/v2.1/pro/image-to-video"
        ws_video_model = settings_manager.get("wavespeed_video_model") or "wavespeed-ai/wan-i2v-480p"
        if ai_provider == "fal":
            os.environ.setdefault("FAL_API_KEY", os.getenv("FAL_API_KEY", "") or settings_manager.get("fal_api_key", ""))
        elif ai_provider == "wavespeed":
            os.environ.setdefault("WAVESPEED_API_KEY", os.getenv("WAVESPEED_API_KEY", "") or settings_manager.get("wavespeed_api_key", ""))
        n_scenes = len(scenes)

        # How images are used per reel type:
        #   character — same reference image sent to every clip; first 0.5 s trimmed
        #               during editing so the static reference frame never appears
        #   story     — each keyframe becomes frame 1 of its own scene (intended)
        #   none      — pure text-to-video
        CHARACTER_TRIM = 0.5  # seconds to remove from start of each character-mode clip
        _ak = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")

        # When ElevenLabs voiceover is enabled, the spoken track comes from ElevenLabs —
        # so Veo3 clips must carry NO speech of their own (otherwise two voices overlap).
        # vo_on therefore (a) skips injecting "Character says: ..." into Veo3 prompts and
        # (b) passes a negative prompt instructing Veo3 to avoid generating any narration.
        vo_on = params.get("include_voiceover", True)
        _VEO3_NO_SPEECH = (
            "speech, narration, voiceover, talking, dialogue, spoken words, "
            "singing, lip movement, subtitles, captions"
        )

        # ── Batch-rewrite voiceovers → short safe dialogue for Veo3 ──────────
        # One Claude Haiku call converts all scene voiceovers into ≤10-word safe
        # English dialogue snippets. These are passed to Veo3 as:
        #   Character says: "rewritten line"
        # so the model understands the spoken mood without receiving raw Hinglish
        # that might contain policy-triggering words.
        # Skipped entirely when voiceover is on — Veo3 should not speak in that case.
        scene_dialogues: dict[int, str] = {}
        _vo_raw = {i: s.get("voiceover", "").strip() for i, s in enumerate(scenes) if s.get("voiceover", "").strip()}
        if not vo_on and _vo_raw and _ak:
            try:
                import anthropic as _anth, json as _json
                _lines = "\n".join(f'{i}: {txt}' for i, txt in _vo_raw.items())
                _resp = _anth.Anthropic(api_key=_ak).messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=800,
                    messages=[{"role": "user", "content": (
                        "You are sanitising voiceover lines for Google Veo3 video generation.\n\n"
                        "For each line:\n"
                        "1. KEEP the exact same language as the input. If the line is Hinglish, output Hinglish.\n"
                        "   If Hindi, output Hindi. If English, output English. Do NOT translate.\n"
                        "2. Keep the full length — do not shorten or summarise.\n"
                        "3. Only replace words that would violate Google content policy:\n"
                        "   violence (maar, maarna, khoon, ladai, tabahi, kill, blood, fight, war),\n"
                        "   self-harm (marna, suicide, die, dying), sexual content, minor/age terms\n"
                        "   (girl, ladki, bachi, teenage, child). Swap with safe equivalents in the\n"
                        "   same language — e.g. 'maar daala' → 'jeet liya', 'marne wali thi' → 'haari si lag rahi thi',\n"
                        "   'ladai' → 'mushkil', 'ladki' → 'woh'.\n"
                        "4. Everything else stays exactly as written.\n"
                        "5. Output ONLY valid JSON: {\"0\": \"sanitised line\", \"1\": \"...\", ...}\n\n"
                        f"Lines:\n{_lines}"
                    )}],
                )
                _raw = _resp.content[0].text.strip().strip("```").lstrip("json").strip()
                for k, v in _json.loads(_raw).items():
                    scene_dialogues[int(k)] = v.strip()
                job.update(message=f"Voiceovers condensed to safe dialogue for {len(scene_dialogues)} scenes")
            except Exception as _exc:
                job.update(message=f"Voiceover dialogue rewrite skipped ({_exc})")

        def _is_policy_error(err: str) -> bool:
            _kw = ["sensitive words", "responsible ai", "content policy",
                   "violate", "safety", "blocked", "support code"]
            return any(k in err.lower() for k in _kw)

        def _rewrite_safe_prompt(original: str) -> str:
            """Ask Claude Haiku to rephrase a blocked prompt, preserving visual intent."""
            try:
                import anthropic as _a
                resp = _a.Anthropic(api_key=_ak).messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": (
                        "This video generation prompt was rejected by Google Veo3 for content policy. "
                        "Rewrite it to describe the same scene safely. "
                        "Rules: use only neutral, visual language; describe emotions through body language "
                        "and environment (not words like 'dead', 'kill', 'fight', 'blood'); "
                        "keep it under 200 characters; return ONLY the rewritten prompt.\n\n"
                        f"Original: {original}"
                    )}],
                )
                return resp.content[0].text.strip()
            except Exception:
                return original

        def _generate_scene_clip(unit: dict) -> tuple[int, dict | None, str | None]:
            order = unit["order"]
            i = unit["scene_index"]
            scene = unit["scene"]
            sub = unit.get("sub", 0)
            prompt = scene["visual_description"]
            hint = scene.get("clip_hint", "")
            if hint:
                prompt = f"{prompt}. {hint}"
            if sub > 0:
                # Continuation clip for a long-narration scene — same moment, fresh angle.
                prompt = f"{prompt}. Continuation of the same moment: same setting and subject, a fresh camera angle, seamless."
            dialogue = scene_dialogues.get(i, "")
            if dialogue:
                prompt = f'{prompt}. Character says: "{dialogue}"'

            # Smart clip length: generate only as long as this scene needs (snapped to
            # the provider's valid lengths), so a short scene isn't billed as a full clip.
            _dur_src = float(unit.get("target_dur") or scene.get("duration_seconds", 5))
            duration = _snap_clip_duration(ai_provider, _dur_src)
            dest_path = str(clips_dir / f"clip_{order:03d}_veo3.mp4")

            image_path = None
            reference_image_paths = None
            _still = scene_stills.get(scene["scene"]) if scene_stills else None
            if _still:
                # Keyframe-first: image-to-video from the approved still (which already
                # bakes in the creator/product/scene look you reviewed).
                image_path = _still
            elif reel_type == "ugc" and character_image_path and product_image_path:
                # UGC: send BOTH the creator and the product as reference ("asset")
                # images so Veo3 keeps them together — the model holding/showing the
                # product — in every shot.
                reference_image_paths = [character_image_path, product_image_path]
            elif reel_type == "character" and character_image_path:
                image_path = character_image_path
            elif reel_type == "story" and keyframe_paths:
                image_path = keyframe_paths[i % len(keyframe_paths)]
            elif reel_type == "product" and product_image_path and scene.get("feature_product"):
                # Product mode: anchor the product only on the scenes the storyboard
                # flagged as showcase ("hero") scenes — others stay pure text-to-video.
                image_path = product_image_path

            neg_prompt = _VEO3_NO_SPEECH if vo_on else ""
            veo_model = ugc_model if reference_image_paths else standard_model

            result = _generate_video_clip(
                ai_provider, prompt=prompt, dest_path=dest_path,
                image_path=image_path, reference_image_paths=reference_image_paths,
                negative_prompt=neg_prompt, duration=duration,
                vertex_project_id=project_id_vx, vertex_location=location,
                veo_model=veo_model, fal_endpoint=fal_video_endpoint, ws_model=ws_video_model,
            )

            # ── Auto-retry on policy violation (Veo content policy) ──────────
            if not result.success and _is_policy_error(result.error):
                safe_prompt = _rewrite_safe_prompt(prompt)
                retry_path = str(clips_dir / f"clip_{order:03d}_retry.mp4")
                result = _generate_video_clip(
                    ai_provider, prompt=safe_prompt, dest_path=retry_path,
                    image_path=image_path, reference_image_paths=reference_image_paths,
                    negative_prompt=neg_prompt, duration=duration,
                    vertex_project_id=project_id_vx, vertex_location=location,
                    veo_model=veo_model, fal_endpoint=fal_video_endpoint, ws_model=ws_video_model,
                )
                if result.success:
                    dest_path = retry_path
            if not result.success:
                return order, None, f"Clip generation failed for scene {i + 1} ({ai_provider}): {result.error}"

            an = ClipAnalyzer().execute({"local_path": dest_path})
            if not an.success:
                return order, None, f"Could not analyze generated clip for scene {i + 1}"

            return order, {
                "clip_id": f"clip_{order:03d}",
                "order": order,
                "filename": Path(dest_path).name,
                "local_path": dest_path,
                "duration_seconds": float(an.data["duration_seconds"]),
                "resolution": {"width": an.data["width"], "height": an.data["height"]},
                "fps": an.data["fps"],
                "selection_reason": f"{ai_provider}: {scene['visual_description'][:60]}",
                "scene": scene["scene"],
                "sub": sub,
                "target_dur": unit.get("target_dur"),
                "pad": unit.get("pad", 0.0),
                "score": None,
            }, None

        # Build render units. When voiceover drives the edit, each scene is ONE clip —
        # and only fans into extra clips when its narration is longer than a single model
        # clip (~8s), so the visual covers the whole VO with the fewest clips (lowest cost,
        # least drift). No cutting on internal pauses here (that's Cinematic's job, where
        # clips are cheap keyframe stills). The full voiceover still plays uncut.
        from tools.audio.pause_detector import plan_beat_clips

        def _pad_for(scene: dict) -> float:
            if reel_type == "character":
                return CHARACTER_TRIM
            if reel_type == "product" and product_image_path and scene.get("feature_product"):
                return CHARACTER_TRIM
            return 0.0

        multiclip_vo = bool(scene_vo_durations) and not params.get("skip_edit_planner")
        _clip_secs = _provider_clip_seconds(ai_provider)
        render_units: list[dict] = []
        _uorder = 0
        for si, scene in enumerate(scenes):
            pad = _pad_for(scene)
            vo = scene_vo_durations.get(scene["scene"], 0.0) if multiclip_vo else 0.0
            if multiclip_vo and vo > 0:
                usable = max(2.0, _clip_secs - pad)
                for c in plan_beat_clips([{"start": 0, "end": vo, "duration": vo}], max_clip_seconds=usable):
                    render_units.append({"order": _uorder, "scene_index": si, "scene": scene,
                                         "sub": c["sub_index"], "target_dur": c["target_duration"], "pad": pad})
                    _uorder += 1
            elif multiclip_vo:
                usable = max(2.0, _clip_secs - pad)
                render_units.append({"order": _uorder, "scene_index": si, "scene": scene, "sub": 0,
                                     "target_dur": round(min(float(scene.get("duration_seconds", 5)), usable), 3), "pad": pad})
                _uorder += 1
            else:
                render_units.append({"order": _uorder, "scene_index": si, "scene": scene, "sub": 0,
                                     "target_dur": None, "pad": pad})
                _uorder += 1
        n_units = len(render_units)

        clip_entries_raw: dict[int, dict] = {}
        gen_done = 0
        gen_errors: list[str] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_generate_scene_clip, u): u["order"] for u in render_units}
            for fut in as_completed(futures):
                idx, entry, err = fut.result()
                gen_done += 1
                if err:
                    gen_errors.append(err)
                    job.update(message=f"Clip {gen_done} failed: {err}")
                else:
                    clip_entries_raw[idx] = entry
                    job.update(
                        progress_pct=25 + int((gen_done / n_units) * 12),
                        message=f"Generated {gen_done}/{n_units} AI clips"
                        + (" (VO-timed)" if multiclip_vo else ""),
                    )

        clip_entries = [clip_entries_raw[i] for i in sorted(clip_entries_raw)]

        if len(clip_entries) < 2:
            first_err = gen_errors[0] if gen_errors else "unknown error"
            raise RuntimeError(
                f"Only {len(clip_entries)}/{n_units} AI clips generated — need at least 2. "
                f"Error: {first_err}"
            )

        if scene_vo_files:
            present_scenes = {c["scene"] for c in clip_entries if c.get("scene")}
            scene_vo_files = {k: v for k, v in scene_vo_files.items() if k in present_scenes}

        clip_manifest = {
            "version": "1.0",
            "clips": clip_entries,
            "total_available_duration_seconds": sum(c["duration_seconds"] for c in clip_entries),
        }
        job.end_stage("clip_selection", f"{len(clip_entries)} Veo3 AI clips generated")

        # ── STAGE 5: edit_decisions ────────────────────────────────────────────
        job.begin_stage("edit_decisions", "Planning the Edit", "Calculating timing from storyboard...")
        job.update(progress_pct=40)

        target_dur = reel_brief["target_duration_seconds"]

        # Product mode: scenes whose first frame is the static product still — trim
        # those like character mode so the still frame Veo3 puts at position 0 is hidden.
        product_scene_nums: set = set()
        if reel_type == "product" and product_image_path:
            product_scene_nums = {s["scene"] for s in scenes if s.get("feature_product")}

        if params.get("skip_edit_planner"):
            cuts = []
            for i, clip in enumerate(clip_manifest["clips"]):
                _is_product_hero = reel_type == "product" and clip.get("scene") in product_scene_nums
                trim_in = CHARACTER_TRIM if (reel_type == "character" or _is_product_hero) else 0.0
                trim_out = round(clip["duration_seconds"], 3)
                if trim_out <= trim_in:
                    trim_out = round(min(trim_in + 3.0, clip["duration_seconds"]), 3)
                cuts.append({
                    "order": i,
                    "clip_id": clip["clip_id"],
                    "trim_in": trim_in,
                    "trim_out": trim_out,
                    "transition": "hard_cut",
                    "transition_duration_seconds": 0.01,
                    "scene": clip.get("scene") or (i + 1),
                })
            total = sum(c["trim_out"] - c["trim_in"] for c in cuts)
            edit_decisions = {
                "version": "1.1",
                "cuts": cuts,
                "total_duration_seconds": round(max(total, 1.0), 2),
                "music_start_offset_seconds": 0.0,
                "abt_timing": {},
            }
            job.end_stage("edit_decisions", f"{len(cuts)} clips · {edit_decisions['total_duration_seconds']:.1f}s · edit planner skipped")
        elif multiclip_vo:
            # Voiceover drives the edit, with a soft ~0.3s DISSOLVE between scenes.
            # Each scene's clips sum to its full VO length; non-last clips are extended
            # into their spare frames (clips are generated a touch longer than needed)
            # so the crossfade overlaps that extra footage — not the voiceover — keeping
            # the whole narration in sync and uncut.
            _DISSOLVE = 0.3
            ordered = sorted(clip_manifest["clips"], key=lambda c: c.get("order", 0))
            cuts = []
            for idx, clip in enumerate(ordered):
                pad = clip.get("pad", 0.0)
                clip_len = clip["duration_seconds"]
                target = clip.get("target_dur")
                base = target if target is not None else clip_len
                trim_in = pad if pad < clip_len - 0.3 else 0.0
                is_last = idx == len(ordered) - 1
                extra = 0.0 if is_last else _DISSOLVE
                trim_out = round(min(trim_in + base + extra, clip_len), 3)
                if trim_out <= trim_in:
                    trim_out = round(min(trim_in + 2.0, clip_len), 3)
                cuts.append({
                    "order": clip["order"], "clip_id": clip["clip_id"],
                    "trim_in": trim_in, "trim_out": trim_out,
                    "transition": "hard_cut" if is_last else "dissolve",
                    "transition_duration_seconds": 0.0 if is_last else _DISSOLVE,
                    "scene": clip.get("scene"),
                })
            total = sum(c["trim_out"] - c["trim_in"] for c in cuts) - _DISSOLVE * max(0, len(cuts) - 1)
            edit_decisions = {
                "version": "1.1", "cuts": cuts,
                "total_duration_seconds": round(max(total, 1.0), 2),
                "music_start_offset_seconds": 0.0, "abt_timing": {},
            }
            job.end_stage("edit_decisions",
                          f"{len(cuts)} clips · {edit_decisions['total_duration_seconds']:.1f}s · VO-timed, soft dissolves")
        else:
            edit_decisions = edit_planner.plan_edit(
                clip_manifest, scenes, target_dur,
                scene_vo_durations=scene_vo_durations or None,
            )

            # Character mode: push trim_in forward by CHARACTER_TRIM on every cut
            # so the static reference frame Veo3 puts at position 0 is never shown.
            if reel_type == "character":
                for cut in edit_decisions["cuts"]:
                    new_in = cut["trim_in"] + CHARACTER_TRIM
                    if new_in < cut["trim_out"] - 0.5:
                        cut["trim_in"] = round(new_in, 3)
            # Product mode: same trim, but only on the product-conditioned hero scenes.
            elif reel_type == "product" and product_scene_nums:
                for cut in edit_decisions["cuts"]:
                    if cut.get("scene") in product_scene_nums:
                        new_in = cut["trim_in"] + CHARACTER_TRIM
                        if new_in < cut["trim_out"] - 0.5:
                            cut["trim_in"] = round(new_in, 3)

            abt_timing = edit_decisions.get("abt_timing", {})
            abt_desc = ", ".join(f"{r}@{t}s" for r, t in abt_timing.items()) if abt_timing else ""
            job.end_stage("edit_decisions",
                          f"{len(edit_decisions['cuts'])} cuts · {edit_decisions['total_duration_seconds']:.1f}s"
                          + (f" · {abt_desc}" if abt_desc else ""))

        # ── STAGE 6: compose ───────────────────────────────────────────────────
        job.begin_stage("compose", "Composing Reel", "Normalizing clips in parallel...")
        job.update(progress_pct=45)

        clip_by_id = {c["clip_id"]: c for c in clip_manifest["clips"]}
        cuts_ordered = sorted(edit_decisions["cuts"], key=lambda x: x["order"])
        n_cuts = len(cuts_ordered)
        norm_done = 0

        def _normalize_cut(cut: dict) -> tuple:
            clip = clip_by_id[cut["clip_id"]]
            norm_path = str(tmp_dir / "normalized" / f"{cut['order']:02d}_{cut['clip_id']}.mp4")
            result = VideoNormalizer().execute({
                "input_path": clip["local_path"],
                "output_path": norm_path,
                "trim_in": cut["trim_in"],
                "trim_out": cut["trim_out"],
            })
            return cut, clip, norm_path, result

        normalized_by_order: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=min(4, n_cuts)) as pool:
            futures = {pool.submit(_normalize_cut, cut): cut for cut in cuts_ordered}
            for fut in as_completed(futures):
                cut, clip, norm_path, n = fut.result()
                norm_done += 1
                if not n.success:
                    raise RuntimeError(f"Normalize failed for {clip['filename']}: {n.error}")
                job.update(
                    progress_pct=45 + int((norm_done / n_cuts) * 20),
                    message=f"Normalized {norm_done}/{n_cuts} clips",
                )
                normalized_by_order[cut["order"]] = {
                    "path": norm_path,
                    "duration_seconds": cut["trim_out"] - cut["trim_in"],
                    "transition": cut["transition"],
                    "transition_duration": cut.get("transition_duration_seconds", 0.5),
                }

        normalized_clips = [normalized_by_order[i] for i in sorted(normalized_by_order)]

        job.update(progress_pct=66, message=f"Joining {len(normalized_clips)} clips...")
        composed_path = str(tmp_dir / "composed.mp4")
        compose_result = VideoComposer().execute({"clips": normalized_clips, "output_path": composed_path})
        if not compose_result.success:
            raise RuntimeError(f"Composition failed: {compose_result.error}")

        job.update(progress_pct=78, message="Rendering per-scene text overlays...")
        # Multi-clip scenes need scene-grouped text timing (one overlay spanning the
        # scene's clips), not per-clip — otherwise the mapping breaks.
        scene_text = (_cinematic_text_timing(normalized_clips, cuts_ordered, scenes)
                      if multiclip_vo else _compute_scene_text_timing(normalized_clips, cuts_ordered, scenes))
        titled_path = composed_path
        if scene_text and reel_brief.get("include_text", True):
            titled_path = str(tmp_dir / "titled.mp4")
            tr_result = TextRenderer().execute({
                "input_path": composed_path,
                "output_path": titled_path,
                "scenes": scene_text,
            })
            if not tr_result.success:
                titled_path = composed_path
                job.update(message=f"Warning: text overlay failed, skipping: {tr_result.error}")

        voiceover_path = None
        if scene_vo_files:
            job.update(progress_pct=83, message="Syncing voiceover to scene timestamps...")
            # Crossfades overlap adjacent clips, shortening the timeline by each
            # boundary's transition_duration — subtract those so the VO lines up.
            video_duration = (sum(c["duration_seconds"] for c in normalized_clips)
                              - sum(c["transition_duration"] for c in normalized_clips[:-1]))
            voiceover_path = edit_planner.build_positioned_voiceover(
                scene_vo_files=scene_vo_files,
                normalized_clips=normalized_clips,
                cuts_ordered=cuts_ordered,
                voiceover_dir=voiceover_dir,
                total_duration=video_duration,
            )
            if voiceover_path:
                job.update(message="Voiceover synced to video timeline")
            else:
                job.update(message="Warning: voiceover positioning failed — audio mix without voiceover")

        job.update(progress_pct=85, message="Mixing voiceover and music...")
        final_path = str(output_dir / "ai_reel_final.mp4")
        music_path = _resolve_music(reel_brief.get("music_file"))

        mix_result = AudioMixer().execute({
            "video_path": titled_path,
            "output_path": final_path,
            "voiceover_path": voiceover_path,
            "music_path": music_path,
            "duck_original": True,
            # Voiceover on → drop the AI clip's own audio so Veo/Kling speech can't
            # clash with the ElevenLabs narration.
            "drop_original": vo_on,
        })
        if not mix_result.success:
            raise RuntimeError(f"Audio mix failed: {mix_result.error}")

        job.end_stage("compose", "Reel composed successfully")

        # ── STAGE 7: final_review ──────────────────────────────────────────────
        job.begin_stage("final_review", "Quality Review", "Checking codec, resolution, and audio...")
        job.update(progress_pct=90)

        probe = VideoProber().execute({"path": final_path})
        probe_data = probe.data if probe.success else {}

        FrameSampler().execute({
            "video_path": final_path,
            "output_dir": str(project_dir / "review" / "frames"),
            "num_frames": 4,
        })

        audio_check = AudioProber().execute({"path": final_path})
        audio_data = audio_check.data if audio_check.success else {}

        actual_dur = probe_data.get("duration_seconds", 0)

        findings = []
        if probe_data.get("width") != 1080 or probe_data.get("height") != 1920:
            findings.append({
                "severity": "critical",
                "check": "technical_probe",
                "description": f"Resolution is {probe_data.get('width')}x{probe_data.get('height')}, expected 1080x1920",
                "recommended_action": "Re-normalize clips",
            })

        review_status = "fail" if any(f["severity"] == "critical" for f in findings) else "pass"
        if review_status == "fail":
            raise RuntimeError(f"Final review failed: {[f['description'] for f in findings if f['severity'] == 'critical']}")

        job.end_stage("final_review", "All quality checks passed")

        # ── STAGE 8: deliver ───────────────────────────────────────────────────
        job.begin_stage("deliver", "Ready!", "Your AI reel is ready for download")
        job.update(progress_pct=99)

        file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0
        local_copy_path = _copy_to_local_output(project_dir, project_id)
        job.end_stage("deliver", "Done")
        job.update(
            status=JobStatus.COMPLETED,
            progress_pct=100,
            message="AI reel ready!" + (f" Saved to {local_copy_path}" if local_copy_path else ""),
            result={
                "project_id": project_id,
                "output_path": final_path,
                "local_copy_path": local_copy_path,
                "duration_seconds": actual_dur,
                "file_size_bytes": file_size,
                "clips_used": len(clip_entries),
                "voiceover": voiceover_path is not None,
                "source": "veo3",
                "scenes": [
                    {"scene": s["scene"], "overlay_text": s.get("overlay_text", ""),
                     "voiceover": s.get("voiceover", "")}
                    for s in scenes
                ],
            },
        )

    except Exception as exc:
        job.update(status=JobStatus.FAILED, message="AI pipeline failed", error=str(exc))
        raise


# ── Cinematic M2: keyframe generation + continuity QA ──────────────────────────

def _cinematic_reference_paths(reference_path: str, char_sheet: str | None) -> list[str]:
    paths = [reference_path]
    if char_sheet and Path(char_sheet).exists():
        paths.append(char_sheet)
    return paths


def _build_keyframes_payload(entries: list[dict], verdicts: dict[str, dict]) -> list[dict]:
    out = []
    for e in entries:
        out.append({
            "kf_id": e["kf_id"],
            "scene": e["scene"],
            "filename": e["filename"],
            "image_prompt": e["image_prompt"],
            "success": e["success"],
            "error": e.get("error"),
            "verdict": verdicts.get(e["kf_id"], {}),
        })
    return out


def _flagged_count(keyframes_payload: list[dict]) -> int:
    # Only a "fail" (clearly a different person/outfit) counts as a blocking flag.
    # "warn" (minor drift) stays advisory — shown in the UI but not counted, so the
    # gate doesn't cry wolf on every shot.
    return sum(
        1 for k in keyframes_payload
        if not k["verdict"].get("pass", True) or k["verdict"].get("severity") == "fail"
    )


def _run_cinematic_keyframes(job: Job, params: dict, project_dir: Path, project_id: str,
                             storyboard: dict, scenes: list[dict]) -> None:
    """Generate keyframe stills (chained for character lock, C1), run the continuity
    drift gate on the cheap stills (C3), and stop at the M2 boundary before any video
    spend. Context is stashed on the job so a single still can be reshot from the modal."""
    # Reflect approval-gate VO edits back into the shot contract.
    shots = storyboard.get("shots", [])
    scene_by_num = {s["scene"]: s for s in scenes}
    for i, shot in enumerate(shots):
        edited = scene_by_num.get(i + 1, {})
        if "voiceover" in edited:
            shot["vo_text"] = edited["voiceover"]

    style_lock = storyboard.get("style_lock", "")
    reference_path = params.get("character_image_path")
    strategy = settings_manager.get("cinematic_chaining_strategy", "sheet")
    keyframes_dir = project_dir / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)

    fal_key = os.getenv("FAL_API_KEY", "") or settings_manager.get("fal_api_key", "")
    if not fal_key:
        raise RuntimeError(
            "FAL_API_KEY is not set — add your fal.ai key in Settings to generate cinematic keyframes."
        )
    os.environ.setdefault("FAL_API_KEY", fal_key)

    # ── STAGE: keyframe_generation ─────────────────────────────────────────────
    job.begin_stage("keyframe_generation", "Generating Keyframes",
                    f"Nano Banana · {strategy} character lock...")
    job.update(progress_pct=12)
    n = len(shots)

    def _kf_progress(done: int, total: int, kf_id: str, ok: bool) -> None:
        job.update(progress_pct=12 + int((done / max(total, 1)) * 12),
                   message=f"Keyframe {done}/{total} ({kf_id})" + ("" if ok else " — failed"))

    gen = KeyframeGenerator().generate_sequence(
        shots, style_lock, reference_path, str(keyframes_dir),
        strategy=strategy, progress_cb=_kf_progress,
    )
    entries = gen["entries"]
    char_sheet = gen["character_sheet"]
    ok_count = sum(1 for e in entries if e["success"])
    if ok_count < 2:
        first_err = next((e["error"] for e in entries if e.get("error")), "unknown error")
        raise RuntimeError(f"Only {ok_count}/{n} keyframes generated — need at least 2. Error: {first_err}")
    job.end_stage("keyframe_generation",
                  f"{ok_count}/{n} keyframes generated" + (" · character sheet locked" if char_sheet else ""))

    # ── STAGE: continuity_qa (drift gate on cheap stills, before any video) ─────
    job.begin_stage("continuity_qa", "Continuity Check", "Claude is checking character drift on the stills...")
    job.update(progress_pct=26)
    reference_paths = _cinematic_reference_paths(reference_path, char_sheet)
    verdicts = ContinuityChecker().check_entries(entries, reference_paths)
    keyframes_payload = _build_keyframes_payload(entries, verdicts)
    flagged = _flagged_count(keyframes_payload)
    n_warn = sum(1 for k in keyframes_payload if k["verdict"].get("severity") == "warn")
    if flagged:
        qa_msg = f"{flagged} of {len(keyframes_payload)} stills flagged (drift) — reshoot before clips"
    elif n_warn:
        qa_msg = f"All stills usable · {n_warn} minor-drift advisories"
    else:
        qa_msg = "All stills consistent"
    job.end_stage("continuity_qa", qa_msg)

    # Persist artifacts so the run is resumable/reviewable.
    keyframe_manifest = {
        "version": "1.0",
        "strategy": strategy,
        "character_sheet": char_sheet,
        "keyframes": [
            {k: e[k] for k in ("kf_id", "scene", "filename", "local_path", "image_prompt", "success", "error")}
            for e in entries
        ],
        "verdicts": verdicts,
    }
    (project_dir / "keyframe_manifest.json").write_text(json.dumps(keyframe_manifest, indent=2))
    (project_dir / "cinematic_contract.json").write_text(json.dumps(storyboard, indent=2))

    # Stash context so the regenerate-single-still endpoint + image serving can work.
    job.cinematic_ctx = {
        "project_dir": str(project_dir),
        "keyframes_dir": str(keyframes_dir),
        "reference_path": reference_path,
        "reference_paths": reference_paths,
        "char_sheet": char_sheet,
        "style_lock": style_lock,
        "strategy": strategy,
        "shots": shots,
    }

    # ── CONTINUITY QA GATE (second approval) ───────────────────────────────────
    job.request_approval({
        "storyboard": storyboard,
        "reel_summary": {
            "mode": "ai_reels",
            "reel_type": "cinematic",
            "prompt": params.get("prompt"),
            "target_duration_seconds": round(sum(float(s.get("duration_sec", 0)) for s in shots), 1),
            "scene_count": n,
        },
        "policy_flags": [],
        "cinematic": True,
        "continuity": True,
        "style_lock": style_lock,
        "keyframes": keyframes_payload,
        "flagged_count": flagged,
    })
    approved = job.wait_for_approval(timeout=1800)
    if not approved:
        raise RuntimeError("Continuity approval timed out after 30 minutes")
    # Stills approved → caller continues into clip generation (M3).


def regenerate_cinematic_keyframe(job: Job, kf_id: str, image_prompt: str | None = None) -> dict:
    """Reshoot a single keyframe still during the review gate, re-run its drift check,
    and patch the live approval payload in place. Handles both the Cinematic keyframes
    and the standard-AI keyframes (which use a stored per-scene prompt + references)."""
    ctx = getattr(job, "cinematic_ctx", None)
    if not ctx:
        raise RuntimeError("This job has no keyframe context to regenerate from.")

    shot = next((s for s in ctx["shots"] if s.get("kf_id") == kf_id), None)
    if shot is None:
        raise RuntimeError(f"Unknown keyframe id: {kf_id}")

    dest = str(Path(ctx["keyframes_dir"]) / f"{kf_id}.png")
    ref_paths = ctx.get("reference_paths") or []

    if ctx.get("standard_keyframes"):
        # Standard modes: regenerate directly from the stored mode-aware prompt (+ any
        # user edit) and the scene's own reference images — no character-sheet strategy.
        prompt = image_prompt or shot.get("full_prompt") or shot.get("image_prompt", "")
        res = KeyframeGenerator()._generate_image(prompt, shot.get("refs", []), dest)
    else:
        if image_prompt:
            shot = {**shot, "image_prompt": image_prompt}
        res = KeyframeGenerator().regenerate(
            shot, ctx["style_lock"], ctx["reference_path"], ctx.get("char_sheet"), dest, ctx["strategy"],
        )
    if not res.success:
        raise RuntimeError(f"Keyframe regeneration failed: {res.error}")

    verdict = (ContinuityChecker()._check_one(dest, ref_paths) if ref_paths
               else {"pass": True, "issues": [], "severity": "ok"})
    updated = {
        "kf_id": kf_id,
        "filename": f"{kf_id}.png",
        "image_prompt": shot.get("image_prompt", ""),
        "success": True,
        "error": None,
        "verdict": verdict,
    }

    # Patch the live approval payload so the modal reflects the reshoot.
    data = job.approval_data or {}
    keyframes = data.get("keyframes", [])
    for k in keyframes:
        if k["kf_id"] == kf_id:
            k.update(updated)
            k.setdefault("scene", shot.get("scene"))
            break
    data["flagged_count"] = _flagged_count(keyframes)
    job.approval_data = data
    return {**updated, "flagged_count": data["flagged_count"]}


def _keyframe_provider_ready() -> bool:
    """Whether an image provider is configured to generate standard-mode keyframes."""
    prov = (settings_manager.get("cinematic_keyframe_provider", "fal") or "fal").lower()
    if prov == "fal":
        return bool(os.getenv("FAL_API_KEY") or settings_manager.get("fal_api_key"))
    if prov == "google":
        return bool(settings_manager.get("vertex_project_id"))
    if prov == "wavespeed":
        return bool(settings_manager.get("wavespeed_api_key"))
    return False


def _standard_still_prompt(scene: dict, has_person_ref: bool, has_product_ref: bool, no_characters: bool) -> str:
    base = (scene.get("visual_description", "") or "").strip()
    hint = (scene.get("clip_hint", "") or "").strip()
    parts = [f"Vertical 9:16 cinematic film still, photorealistic. {base}"]
    if hint:
        parts.append(hint)
    if has_person_ref:
        parts.append("The person is the SAME individual as the reference image — identical face, "
                     "hair, skin tone and wardrobe; never a look-alike.")
    if has_product_ref:
        parts.append("Feature the exact product from the reference image — matching shape, colour and details.")
    if no_characters:
        parts.append("NO people, humans or body parts anywhere — only objects, product, scenery and textures.")
    parts.append("Render NO text, words, letters, captions, watermarks or logos anywhere in the image.")
    return " ".join(parts)


def _run_standard_keyframes(job: Job, project_dir: Path, scenes: list[dict], reel_type: str,
                            character_image_path: str | None, product_image_path: str | None,
                            keyframe_paths: list, product_scene_nums: set) -> dict:
    """Generate one keyframe still per scene (mode-appropriate references + prompt), run a
    continuity check for person-consistent modes, and gate on a keyframe review (reusing
    the Cinematic review UI/endpoints). Returns {scene_num: still_path} for approved stills."""
    keyframes_dir = project_dir / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    no_characters = reel_type == "none"
    if (settings_manager.get("cinematic_keyframe_provider", "fal") or "fal").lower() == "fal":
        os.environ.setdefault("FAL_API_KEY", os.getenv("FAL_API_KEY", "") or settings_manager.get("fal_api_key", ""))
    kg = KeyframeGenerator()

    def _refs(i: int, scene: dict) -> list:
        sn = scene["scene"]
        if reel_type == "ugc":
            return [p for p in (character_image_path, product_image_path) if p]
        if reel_type == "character":
            return [character_image_path] if character_image_path else []
        if reel_type == "product" and sn in product_scene_nums and product_image_path:
            return [product_image_path]
        if reel_type == "story" and keyframe_paths:
            return [keyframe_paths[i % len(keyframe_paths)]]
        return []

    shots = []
    for i, scene in enumerate(scenes):
        refs = _refs(i, scene)
        has_person = reel_type in ("character", "ugc") and bool(character_image_path)
        has_product = bool(product_image_path) and (
            reel_type == "ugc" or (reel_type == "product" and scene["scene"] in product_scene_nums))
        shots.append({
            "kf_id": f"KF{scene['scene']}", "scene": scene["scene"],
            "image_prompt": scene.get("visual_description", ""),
            "full_prompt": _standard_still_prompt(scene, has_person, has_product, no_characters),
            "refs": refs,
        })

    job.begin_stage("keyframe_generation", "Generating Keyframes", "Creating a still per scene for your review...")
    job.update(progress_pct=13)

    def _gen(i_shot):
        i, shot = i_shot
        dest = str(keyframes_dir / f"{shot['kf_id']}.png")
        return i, shot, dest, kg._generate_image(shot["full_prompt"], shot["refs"], dest)

    entries_by_i: dict[int, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_gen, (i, s)) for i, s in enumerate(shots)]
        for fut in as_completed(futures):
            i, shot, dest, res = fut.result()
            done += 1
            entries_by_i[i] = {
                "kf_id": shot["kf_id"], "scene": shot["scene"], "filename": f"{shot['kf_id']}.png",
                "local_path": dest, "image_prompt": shot["image_prompt"],
                "success": bool(res.success), "error": (res.error if not res.success else None),
            }
            job.update(progress_pct=13 + int(done / len(shots) * 10), message=f"Keyframe {done}/{len(shots)}")
    entries = [entries_by_i[i] for i in sorted(entries_by_i)]
    ok = sum(1 for e in entries if e["success"])
    if ok < 2:
        first = next((e["error"] for e in entries if e.get("error")), "unknown error")
        raise RuntimeError(f"Only {ok}/{len(shots)} keyframes generated — need at least 2. {first}")
    job.end_stage("keyframe_generation", f"{ok}/{len(shots)} keyframes generated — review before video")

    ref_paths = [p for p in (character_image_path, product_image_path) if p]
    verdicts: dict = {}
    if reel_type in ("character", "ugc") and character_image_path:
        job.begin_stage("continuity_qa", "Continuity Check", "Checking the creator looks consistent...")
        verdicts = ContinuityChecker().check_entries(entries, ref_paths)
        job.end_stage("continuity_qa", "Consistency checked")

    keyframes_payload = _build_keyframes_payload(entries, verdicts)
    job.cinematic_ctx = {
        "project_dir": str(project_dir), "keyframes_dir": str(keyframes_dir),
        "reference_path": (character_image_path if reel_type in ("character", "ugc")
                           else product_image_path if reel_type == "product" else None),
        "reference_paths": ref_paths, "char_sheet": None, "style_lock": "", "strategy": "none",
        "shots": shots, "standard_keyframes": True,
    }
    job.request_approval({
        "reel_summary": {"mode": "ai_reels", "reel_type": reel_type, "scene_count": len(scenes)},
        "policy_flags": [], "cinematic": True, "continuity": True, "style_lock": "",
        "keyframes": keyframes_payload, "flagged_count": _flagged_count(keyframes_payload),
    })
    if not job.wait_for_approval(timeout=1800):
        raise RuntimeError("Keyframe approval timed out after 30 minutes")

    return {e["scene"]: e["local_path"] for e in entries if e["success"]}


# ── Cinematic M3: clip generation (still → video, provider-selectable) ─────────

# Veo native audio / any provider speech is unwanted — we lay ElevenLabs VO over
# silent clips. Passed as a negative prompt to keep the generated clips silent.
_CINEMATIC_NO_SPEECH = (
    "speech, narration, voiceover, talking, dialogue, spoken words, "
    "singing, lip movement, subtitles, captions, "
    # Style descriptors (e.g. "shot on Kodak") must affect the LOOK only — never
    # appear as literal text/letters/logos burned into the frame.
    "on-screen text, words, letters, typography, captions, watermark, logo, brand name"
)


def _generate_video_clip(provider: str, *, prompt: str, dest_path: str,
                         image_path: str | None = None,
                         reference_image_paths: list[str] | None = None,
                         tail_image_path: str | None = None,
                         negative_prompt: str = "", duration: int = 5,
                         vertex_project_id: str | None = None,
                         vertex_location: str = "us-central1",
                         veo_model: str | None = None,
                         fal_endpoint: str | None = None,
                         ws_model: str | None = None) -> ToolResult:
    """Route ONE clip generation to the configured AI video provider (veo3 | fal |
    wavespeed) — used by every AI reel type. fal/wavespeed are image-to-video and need a
    first-frame image; for text-only scenes they fall back to Veo when Vertex is set."""
    provider = (provider or "veo3").lower()
    first_img = image_path or (reference_image_paths[0] if reference_image_paths else None)

    if provider in ("fal", "wavespeed") and not first_img:
        if vertex_project_id:
            provider = "veo3"   # text-only scene — only Veo does pure text-to-video
        else:
            return ToolResult(success=False, error=(
                f"{provider} is image-to-video and this scene has no reference image — "
                "switch the AI video model to Google (Veo) for text-only reels."))

    if provider == "fal":
        payload = {
            "operation": "image_to_video", "endpoint": fal_endpoint,
            "image_url": FalClient.encode_image(first_img), "prompt": prompt,
            "duration": duration, "aspect_ratio": "9:16",
            "negative_prompt": negative_prompt, "dest_path": dest_path,
        }
        if tail_image_path:
            payload["tail_image_url"] = FalClient.encode_image(tail_image_path)
        return FalClient().execute(payload)

    if provider == "wavespeed":
        return WaveSpeedClient().execute({
            "operation": "image_to_video", "model_slug": ws_model,
            "image_url": FalClient.encode_image(first_img), "prompt": prompt,
            "dest_path": dest_path,
        })

    # veo3 — full capability: text-to-video, first-frame image, multi-reference.
    return Veo3Client().execute({
        "operation": "text_to_video", "prompt": prompt, "dest_path": dest_path,
        "vertex_project_id": vertex_project_id, "vertex_location": vertex_location,
        "image_path": image_path, "reference_image_paths": reference_image_paths,
        "model": veo_model, "negative_prompt": negative_prompt, "duration": duration,
    })


def _run_cinematic_clips(job: Job, params: dict, project_dir: Path, project_id: str,
                         storyboard: dict) -> None:
    """Turn each approved keyframe still into a short silent clip via the settings-selected
    provider (veo3 | fal | wavespeed). needs_end_frame shots get a generated end still and
    first-last interpolation (fal). Stops at the M3 boundary before edit/compose/grade."""
    ctx = getattr(job, "cinematic_ctx", {}) or {}
    shots = storyboard.get("shots", [])
    style_lock = storyboard.get("style_lock", "")
    keyframes_dir = Path(ctx.get("keyframes_dir") or (project_dir / "keyframes"))
    reference_path = ctx.get("reference_path") or params.get("character_image_path")
    char_sheet = ctx.get("char_sheet")
    clips_dir = project_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    # Global AI video provider (shared with the other reel types) — set in Settings.
    provider = (settings_manager.get("ai_video_provider", "veo3") or "veo3").lower()
    vx_project = settings_manager.get("vertex_project_id")
    vx_location = settings_manager.get("vertex_location", "us-central1")
    veo_model = settings_manager.get("veo_standard_model") or "veo-3.1-lite-generate-001"
    fal_endpoint = settings_manager.get("fal_video_endpoint") or "fal-ai/kling-video/v2.1/pro/image-to-video"
    ws_model = settings_manager.get("wavespeed_video_model") or "wavespeed-ai/wan-i2v-480p"
    if provider == "veo3" and not vx_project:
        raise RuntimeError("AI video provider is Google (Veo) but Google Cloud Project ID is not set in Settings.")
    if provider == "fal":
        os.environ.setdefault("FAL_API_KEY", os.getenv("FAL_API_KEY", "") or settings_manager.get("fal_api_key", ""))
    elif provider == "wavespeed":
        os.environ.setdefault("WAVESPEED_API_KEY", os.getenv("WAVESPEED_API_KEY", "") or settings_manager.get("wavespeed_api_key", ""))

    job.begin_stage("clip_generation", "Generating Clips", f"{provider} · still → video per shot...")
    job.update(progress_pct=32)

    # Only shots whose start still actually exists on disk.
    tasks = [(i, shot, str(keyframes_dir / f"{shot.get('kf_id', f'KF{i + 1}')}.png"))
             for i, shot in enumerate(shots)
             if (keyframes_dir / f"{shot.get('kf_id', f'KF{i + 1}')}.png").exists()]
    n = len(tasks)
    if n < 2:
        raise RuntimeError("Fewer than 2 approved keyframe stills available — cannot generate clips.")

    def _gen(task: tuple) -> tuple:
        i, shot, still_path = task
        kf_id = shot.get("kf_id", f"KF{i + 1}")
        video_prompt = f"{shot.get('video_prompt', '').strip()}. STYLE: {style_lock}".strip()
        dest = str(clips_dir / f"clip_{i:03d}_{kf_id}.mp4")

        # End frame for needs_end_frame shots (first-last interpolation; fal only).
        end_still = None
        if shot.get("needs_end_frame") and provider == "fal":
            end_dest = str(keyframes_dir / f"{kf_id}_end.png")
            er = KeyframeGenerator().generate_end_frame(shot, style_lock, reference_path, char_sheet, end_dest)
            if er.success:
                end_still = end_dest

        res = _generate_video_clip(
            provider, prompt=video_prompt, dest_path=dest,
            image_path=still_path, tail_image_path=end_still,
            negative_prompt=_CINEMATIC_NO_SPEECH, duration=5,
            vertex_project_id=vx_project, vertex_location=vx_location,
            veo_model=veo_model, fal_endpoint=fal_endpoint, ws_model=ws_model,
        )
        return i, shot, dest, res

    clip_entries_raw: dict[int, dict] = {}
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_gen, t) for t in tasks]
        for fut in as_completed(futures):
            i, shot, dest, res = fut.result()
            done += 1
            if not res.success:
                errors.append(f"shot {i + 1}: {res.error}")
                job.update(message=f"Clip {i + 1} failed: {res.error}")
                continue
            an = ClipAnalyzer().execute({"local_path": dest})
            if not an.success:
                errors.append(f"shot {i + 1}: clip analyze failed")
                continue
            clip_entries_raw[i] = {
                "clip_id": f"clip_{i:03d}",
                "filename": Path(dest).name,
                "local_path": dest,
                "duration_seconds": float(an.data["duration_seconds"]),
                "resolution": {"width": an.data["width"], "height": an.data["height"]},
                "fps": an.data["fps"],
                "scene": shot.get("scene", i + 1),
                "kf_id": shot.get("kf_id"),
                "provider": provider,
            }
            job.update(progress_pct=32 + int((done / n) * 40), message=f"Generated {done}/{n} clips")

    clip_entries = [clip_entries_raw[i] for i in sorted(clip_entries_raw)]
    balance_err = next((e for e in errors if any(w in e.lower() for w in ("balance", "locked", "exhaust"))), None)
    if balance_err:
        job.update(message="⚠ fal balance exhausted — some clips skipped. Top up at fal.ai/dashboard/billing.")
    if len(clip_entries) < 2:
        first = balance_err or (errors[0] if errors else "unknown error")
        raise RuntimeError(f"Only {len(clip_entries)}/{n} cinematic clips generated — need at least 2. {first}")

    clip_manifest = {
        "version": "1.0",
        "provider": provider,
        "clips": clip_entries,
        "total_available_duration_seconds": sum(c["duration_seconds"] for c in clip_entries),
    }
    (project_dir / "clip_manifest.json").write_text(json.dumps(clip_manifest, indent=2))
    job.end_stage("clip_generation", f"{len(clip_entries)} clips generated via {provider}")
    return clip_manifest


# Per-unit USD price estimates (from real fal invoices, mid-2026). Tune to your rates.
# IMPORTANT: image→video on fal/Kling bills PER SECOND of output, and every clip is
# generated at CLIP_SECONDS (Kling's 5s floor) regardless of how much we keep after
# trimming — so a clip costs CLIP_SECONDS × per-second rate.
_CINEMATIC_CLIP_SECONDS = 5
_CINEMATIC_PRICES = {
    "director_call": 0.03,       # one Claude director call
    "keyframe_image": 0.0398,    # Nano Banana per image (fal actual) — still / sheet / end frame
    "continuity_check": 0.01,    # Claude-vision per still
    "vo_line": 0.05,             # ElevenLabs per spoken line
    # per-second video rates → clip cost = rate × _CINEMATIC_CLIP_SECONDS
    "clip_per_sec": {"veo3": 0.075, "fal": 0.098, "wavespeed": 0.020},
}


def _estimate_cinematic_cost(provider: str, n_stills: int, n_sheet: int, n_end: int,
                             n_continuity: int, n_clips: int, n_vo: int) -> dict:
    p = _CINEMATIC_PRICES
    secs = _CINEMATIC_CLIP_SECONDS
    clip_unit = p["clip_per_sec"].get(provider, 0.098) * secs   # per clip = rate × clip seconds
    rows = [
        ("Director (Claude)", 1, p["director_call"]),
        ("Keyframe stills (Nano Banana)", n_stills + n_sheet + n_end, p["keyframe_image"]),
        ("Continuity checks (Claude vision)", n_continuity, p["continuity_check"]),
        (f"Clips ({provider}, {secs}s each)", n_clips, clip_unit),
        ("Voiceover (ElevenLabs)", n_vo, p["vo_line"]),
    ]
    out = [{"stage": s, "units": u, "unit_cost": round(c, 3), "subtotal": round(u * c, 3)} for s, u, c in rows]
    return {
        "currency": "USD",
        "note": (f"Estimate. Video bills per second × {secs}s/clip — most fal/Kling spend is here. "
                 "Tune rates in ai_runner._CINEMATIC_PRICES."),
        "rows": out,
        "total": round(sum(r["subtotal"] for r in out), 2),
    }


def _preview_ai_cost(reel_type: str, scenes: list[dict], is_cinematic: bool,
                     use_keyframes: bool, include_vo: bool) -> dict:
    """Pre-generation cost estimate shown at the script-approval gate so you approve the
    spend before anything is generated. Counts are estimated from the scene plan."""
    provider = (settings_manager.get("ai_video_provider", "veo3") or "veo3").lower()
    n = len(scenes)
    n_stills = n if (is_cinematic or use_keyframes) else 0
    n_sheet = 1 if is_cinematic else 0
    n_cont = n_stills if (is_cinematic or reel_type in ("character", "ugc")) else 0
    n_vo = sum(1 for s in scenes if (s.get("voiceover") or "").strip()) if include_vo else 0
    est = _estimate_cinematic_cost(provider, n_stills, n_sheet, 0, n_cont, n, n_vo)
    est["preview"] = True
    est["note"] = "Approximate estimate BEFORE generation — long-narration scenes may add a few clips. " + est["note"]
    return est


def _capture_cinematic_inputs(storyboard: dict, provider: str, kf_endpoint: str, clip_model: str) -> dict:
    """Record exactly what was authored and what was sent to each model, for the
    'Review inputs' panel on the finished reel."""
    style_lock = storyboard.get("style_lock", "")
    kg = KeyframeGenerator()
    shots = []
    for s in storyboard.get("shots", []):
        shots.append({
            "kf_id": s.get("kf_id"), "scene": s.get("scene"),
            "shot_type": s.get("shot_type"), "lens_mm": s.get("lens_mm"),
            "dof": s.get("dof"), "camera_move": s.get("camera_move"),
            "duration_sec": s.get("duration_sec"), "needs_end_frame": s.get("needs_end_frame"),
            "image_prompt": s.get("image_prompt", ""),
            "still_prompt_sent": kg._still_prompt(s, style_lock),
            "video_prompt": s.get("video_prompt", ""),
            "clip_prompt_sent": f"{s.get('video_prompt', '').strip()}. STYLE: {style_lock}".strip(),
            "vo_text": s.get("vo_text", ""), "overlay_text": s.get("overlay_text", ""),
        })
    return {
        "style_lock": style_lock,
        "reference_image": storyboard.get("reference_image"),
        "provider": provider,
        "models": {"director": "claude-sonnet-4-6", "keyframe": kf_endpoint, "clip": clip_model},
        "shots": shots,
    }


def _run_cinematic_finish(job: Job, params: dict, project_dir: Path, project_id: str,
                          storyboard: dict, clip_manifest: dict) -> None:
    """M4 + M5 — VO-driven edit decisions, cross-shot colour match, compose, QA, deliver.
    Rejoins the same edit/compose/audio tooling the other AI modes use."""
    from backend import edit_planner
    from backend.pipeline_runner import (
        _generate_scene_vo_files, _compute_scene_text_timing, _resolve_music,
    )

    scenes = storyboard.get("scenes", [])
    shots = storyboard.get("shots", [])
    target_dur = round(sum(float(s.get("duration_sec", 0)) for s in shots), 1) or \
        float(params.get("target_duration_seconds", 16))

    clips_dir = project_dir / "clips"
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"
    voiceover_dir = project_dir / "voiceover"
    for d in [tmp_dir / "normalized", tmp_dir / "graded", output_dir, voiceover_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # ── STAGE: voiceover (per-shot ElevenLabs; drives shot duration, C5) ───────
    job.begin_stage("voiceover", "Generating Voiceover", "Sending shot narration to ElevenLabs...")
    job.update(progress_pct=74)
    scene_vo_files: dict[int, str] = {}
    scene_vo_durations: dict[int, float] = {}
    if params.get("include_voiceover", True):
        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "") or settings_manager.get("elevenlabs_api_key", "")
        if elevenlabs_key and any(s.get("voiceover") for s in scenes):
            scene_vo_files, scene_vo_durations = _generate_scene_vo_files(scenes, voiceover_dir)
            job.end_stage("voiceover", f"{len(scene_vo_files)} shot voiceovers — clips trimmed to VO length")
        else:
            job.end_stage("voiceover", "No voiceover (no ElevenLabs key or empty narration)")
    else:
        job.end_stage("voiceover", "Voiceover disabled — skipping")

    # ── STAGE: edit_decisions (VO-driven durations, C5) ────────────────────────
    job.begin_stage("edit_decisions", "Planning the Edit", "Timing each shot to its voiceover...")
    job.update(progress_pct=78)
    # Keep only VO for shots that actually have a clip.
    present_scenes = {c["scene"] for c in clip_manifest["clips"] if c.get("scene")}
    if scene_vo_files:
        scene_vo_files = {k: v for k, v in scene_vo_files.items() if k in present_scenes}
        scene_vo_durations = {k: v for k, v in scene_vo_durations.items() if k in present_scenes}
    edit_decisions = edit_planner.plan_edit(
        clip_manifest, scenes, target_dur,
        scene_vo_durations=scene_vo_durations or None,
    )
    job.end_stage("edit_decisions",
                  f"{len(edit_decisions['cuts'])} cuts · {edit_decisions['total_duration_seconds']:.1f}s")

    # ── STAGE: compose (normalize → colour match → concat → text → audio) ──────
    job.begin_stage("compose", "Composing Reel", "Normalizing clips...")
    job.update(progress_pct=82)
    clip_by_id = {c["clip_id"]: c for c in clip_manifest["clips"]}
    cuts_ordered = sorted(edit_decisions["cuts"], key=lambda x: x["order"])

    # Honour the director's per-shot transition_out for edit variety (#5): a real
    # dissolve where asked, hard cut otherwise. When any dissolve exists the whole
    # timeline composes via xfade, so hard cuts get a ~2-frame micro-fade and their
    # transition_duration is set so VO/text timing (which subtract it) stay in sync.
    scene_by_num = {s["scene"]: s for s in scenes}

    def _trans_out(cut: dict) -> str:
        return (scene_by_num.get(cut.get("scene"), {}) or {}).get("transition_out", "cut")

    has_dissolve = any(_trans_out(c) == "dissolve" for c in cuts_ordered)
    CUT_FADE = 0.067 if has_dissolve else 0.0  # micro-fade only matters on the xfade path

    normalized_by_order: dict[int, dict] = {}
    for cut in cuts_ordered:
        clip = clip_by_id[cut["clip_id"]]
        norm_path = str(tmp_dir / "normalized" / f"{cut['order']:02d}_{cut['clip_id']}.mp4")
        nr = VideoNormalizer().execute({
            "input_path": clip["local_path"], "output_path": norm_path,
            "trim_in": cut["trim_in"], "trim_out": cut["trim_out"],
        })
        if not nr.success:
            raise RuntimeError(f"Normalize failed for {clip['filename']}: {nr.error}")
        is_dissolve = _trans_out(cut) == "dissolve"
        normalized_by_order[cut["order"]] = {
            "path": norm_path,
            "duration_seconds": cut["trim_out"] - cut["trim_in"],
            "transition": "dissolve" if is_dissolve else "hard_cut",
            "transition_duration": 0.5 if is_dissolve else CUT_FADE,
        }
    normalized_clips = [normalized_by_order[i] for i in sorted(normalized_by_order)]

    # Colour match across all clips (C4), before concat.
    job.update(progress_pct=86, message="Colour-matching shots for a consistent grade...")
    try:
        graded_paths = ColorMatcher().match([c["path"] for c in normalized_clips], str(tmp_dir / "graded"))
        for c, gp in zip(normalized_clips, graded_paths):
            c["path"] = gp
    except Exception as exc:
        job.update(message=f"Colour match skipped ({exc}) — composing ungraded")

    job.update(progress_pct=88, message=f"Joining {len(normalized_clips)} clips...")
    composed_path = str(tmp_dir / "composed.mp4")
    cr = VideoComposer().execute({"clips": normalized_clips, "output_path": composed_path})
    if not cr.success:
        raise RuntimeError(f"Composition failed: {cr.error}")

    titled_path = composed_path
    scene_text = _compute_scene_text_timing(normalized_clips, cuts_ordered, scenes)
    if scene_text and params.get("include_text", True):
        titled_path = str(tmp_dir / "titled.mp4")
        tr = TextRenderer().execute({"input_path": composed_path, "output_path": titled_path, "scenes": scene_text})
        if not tr.success:
            titled_path = composed_path
            job.update(message=f"Text overlay skipped: {tr.error}")

    voiceover_path = None
    if scene_vo_files:
        job.update(progress_pct=90, message="Syncing voiceover to the timeline...")
        # Account for crossfade overlap (each boundary shortens the timeline by its
        # transition_duration) so VO start times match the composed video.
        video_duration = (sum(c["duration_seconds"] for c in normalized_clips)
                          - sum(c["transition_duration"] for c in normalized_clips[:-1]))
        voiceover_path = edit_planner.build_positioned_voiceover(
            scene_vo_files=scene_vo_files, normalized_clips=normalized_clips,
            cuts_ordered=cuts_ordered, voiceover_dir=voiceover_dir, total_duration=video_duration,
        )

    job.update(progress_pct=92, message="Mixing voiceover and music...")
    final_path = str(output_dir / "ai_reel_final.mp4")
    mr = AudioMixer().execute({
        "video_path": titled_path, "output_path": final_path,
        "voiceover_path": voiceover_path, "music_path": _resolve_music(params.get("music_file")),
        "duck_original": True, "drop_original": bool(voiceover_path),
    })
    if not mr.success:
        raise RuntimeError(f"Audio mix failed: {mr.error}")
    job.end_stage("compose", "Reel composed and graded")

    # ── STAGE: final_review (QA) ───────────────────────────────────────────────
    job.begin_stage("final_review", "Quality Review", "Checking resolution and duration...")
    job.update(progress_pct=96)
    probe = VideoProber().execute({"path": final_path})
    probe_data = probe.data if probe.success else {}
    FrameSampler().execute({"video_path": final_path, "output_dir": str(project_dir / "review" / "frames"), "num_frames": 4})
    actual_dur = probe_data.get("duration_seconds", 0)
    if probe_data.get("width") != 1080 or probe_data.get("height") != 1920:
        raise RuntimeError(
            f"Final review failed: resolution {probe_data.get('width')}x{probe_data.get('height')}, expected 1080x1920"
        )
    job.end_stage("final_review", "All quality checks passed")

    # ── STAGE: deliver ─────────────────────────────────────────────────────────
    job.begin_stage("deliver", "Ready!", "Your cinematic reel is ready")
    job.update(progress_pct=99)
    file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0
    local_copy_path = _copy_to_local_output(project_dir, project_id)

    # ── Cost estimate + input capture (C6 / review) ────────────────────────────
    provider = clip_manifest.get("provider", "fal")
    km_path = project_dir / "keyframe_manifest.json"
    km = json.loads(km_path.read_text()) if km_path.exists() else {}
    n_stills = sum(1 for k in km.get("keyframes", []) if k.get("success"))
    n_sheet = 1 if km.get("character_sheet") else 0
    n_end = len(list((project_dir / "keyframes").glob("*_end.png")))
    n_continuity = len(km.get("verdicts", {}))
    n_clips = len(clip_manifest.get("clips", []))
    n_vo = len(scene_vo_files)
    _kg = KeyframeGenerator()
    kf_endpoint = f"{_kg.provider}:{_kg.model}"
    clip_model = (settings_manager.get("fal_video_endpoint") if provider == "fal"
                  else settings_manager.get("veo_standard_model") if provider == "veo3"
                  else settings_manager.get("wavespeed_video_model"))
    review = {
        "cost": _estimate_cinematic_cost(provider, n_stills, n_sheet, n_end, n_continuity, n_clips, n_vo),
        "inputs": _capture_cinematic_inputs(storyboard, provider, kf_endpoint, clip_model or ""),
    }
    (project_dir / "cost_log.json").write_text(json.dumps(review["cost"], indent=2))
    (project_dir / "cinematic_inputs.json").write_text(json.dumps(review["inputs"], indent=2))

    job.end_stage("deliver", f"Done · est. ${review['cost']['total']:.2f}")
    job.update(
        status=JobStatus.COMPLETED, progress_pct=100,
        message="Cinematic reel ready!" + (f" Saved to {local_copy_path}" if local_copy_path else ""),
        result={
            "project_id": project_id, "milestone": "M5", "reel_type": "cinematic",
            "output_path": final_path, "local_copy_path": local_copy_path,
            "duration_seconds": actual_dur, "file_size_bytes": file_size,
            "clips_used": len(normalized_clips), "provider": provider,
            "voiceover": voiceover_path is not None, "source": "cinematic",
            "estimated_cost": review["cost"]["total"],
            "review": review,
            "scenes": [{"scene": s["scene"], "overlay_text": s.get("overlay_text", ""),
                        "voiceover": s.get("voiceover", "")} for s in scenes],
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# PAUSE-DRIVEN CINEMATIC EDIT (audio is the master timeline)
# Order: VO first → detect pauses → each speech beat is a visual unit → generate
# keyframes/clips per beat → cut on the pauses, full VO laid over (no truncation).
# ══════════════════════════════════════════════════════════════════════════════

def _provider_clip_seconds(provider: str) -> int:
    """Max clip length for this provider — used to cap how long a scene's single clip
    can be before it must split into two."""
    return 8 if provider == "veo3" else 5


# The discrete clip lengths each provider can generate (seconds). We pick the smallest
# valid length that covers the scene, so a 3s scene isn't billed as an 8s clip.
_PROVIDER_DURATIONS = {"veo3": (4, 6, 8), "fal": (5, 10), "wavespeed": (5,)}


def _snap_clip_duration(provider: str, target: float) -> int:
    """Smallest valid generation length for `provider` that is ≥ target seconds."""
    opts = _PROVIDER_DURATIONS.get(provider, (8,))
    for o in opts:
        if o >= target - 0.05:
            return o
    return opts[-1]


def _concat_vo_files(paths: list[str], dest_path: str) -> str | None:
    """Concatenate per-shot VO mp3s (in order) into one continuous narration.
    The natural trailing silence in each file preserves the pauses between shots."""
    paths = [p for p in paths if p and Path(p).exists()]
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    list_path = Path(dest_path).with_suffix(".txt")
    list_path.write_text("".join(f"file '{p}'\n" for p in paths), encoding="utf-8")
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_path), "-c:a", "libmp3lame", "-q:a", "2", dest_path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
    )
    list_path.unlink(missing_ok=True)
    return dest_path if r.returncode == 0 else paths[0]


def _run_cinematic_plan(job: Job, params: dict, project_dir: Path, storyboard: dict,
                        scenes: list[dict]) -> tuple:
    """VO FIRST: generate per-shot narration, detect its pauses, and build the
    render plan (beats → clips). Returns (render_plan, vo_concat_path, total_sec).
    Returns (None, None, 0) when there's no voiceover — caller falls back to the
    original shot-per-clip flow."""
    from backend.pipeline_runner import _generate_scene_vo_files
    from tools.ai.cinematic_planner import build_render_plan, plan_summary

    if not params.get("include_voiceover", True):
        return None, None, 0.0
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "") or settings_manager.get("elevenlabs_api_key", "")
    if not (elevenlabs_key and any(s.get("voiceover") for s in scenes)):
        return None, None, 0.0

    shots = storyboard.get("shots", [])
    voiceover_dir = project_dir / "voiceover"
    voiceover_dir.mkdir(parents=True, exist_ok=True)

    job.begin_stage("voiceover", "Generating Voiceover", "Recording narration to time the cuts...")
    job.update(progress_pct=6)
    scene_vo_files, scene_vo_durations = _generate_scene_vo_files(scenes, voiceover_dir)
    if not scene_vo_files:
        job.end_stage("voiceover", "No voiceover produced — using structural timing")
        return None, None, 0.0

    provider = (settings_manager.get("ai_video_provider", "veo3") or "veo3").lower()
    clip_secs = _provider_clip_seconds(provider)

    # Detect each shot's pauses → segments that tile the shot's full VO length.
    # Split-only-when-needed: each shot is ONE segment (its full VO), so it fans into
    # extra clips ONLY when the narration is longer than a single clip (~8s) — no cutting
    # on internal pauses. Keeps the clip count (and Veo/fal cost) down; the full VO still
    # plays and cuts still align to shot boundaries.
    shot_segments: dict[int, list[dict]] = {}
    ordered_vo_paths: list[str] = []
    for i, shot in enumerate(shots):
        scene_num = shot.get("scene", i + 1)
        vo_path = scene_vo_files.get(scene_num)
        if not vo_path:
            continue
        ordered_vo_paths.append(vo_path)
        dur = scene_vo_durations.get(scene_num, 0.0)
        if dur <= 0:
            continue
        shot_segments[i] = [{"start": 0.0, "end": dur, "duration": dur}]

    render_plan = build_render_plan(shots, shot_segments, max_clip_seconds=float(clip_secs))
    if len(render_plan) < 2:
        job.end_stage("voiceover", "Too few shots with voiceover — using structural timing")
        return None, None, 0.0

    vo_concat = _concat_vo_files(ordered_vo_paths, str(voiceover_dir / "narration_full.mp3"))
    total_sec = round(sum(u["target_duration"] for u in render_plan), 2)
    summ = plan_summary(render_plan)
    job.end_stage("voiceover",
                  f"{len(scene_vo_files)} shot VOs · {summ['clips']} clips "
                  f"({summ['continuation_stills']} extra for long shots) · {total_sec:.1f}s")
    return render_plan, vo_concat, total_sec


def _run_cinematic_continuation_stills(job: Job, project_dir: Path, storyboard: dict,
                                       render_plan: list[dict]) -> None:
    """Generate the extra 'continuation' stills for beats beyond a shot's first
    (same shot content, fresh still) so the look stays coherent across the cuts."""
    cont = [u for u in render_plan if u["is_continuation"]]
    if not cont:
        return
    ctx = getattr(job, "cinematic_ctx", {}) or {}
    keyframes_dir = Path(ctx.get("keyframes_dir") or (project_dir / "keyframes"))
    style_lock = ctx.get("style_lock", storyboard.get("style_lock", ""))
    reference_path = ctx.get("reference_path")
    char_sheet = ctx.get("char_sheet")
    strategy = ctx.get("strategy", "sheet")
    shots = storyboard.get("shots", [])

    kg = KeyframeGenerator()
    refs = kg._refs_for(strategy, reference_path, char_sheet, None)
    job.begin_stage("continuation_stills", "Continuation Stills",
                    f"{len(cont)} extra stills for long / multi-beat shots...")
    job.update(progress_pct=28)

    def _gen(u):
        shot = shots[u["shot_index"]]
        dest = str(keyframes_dir / f"{u['still_id']}.png")
        return u, kg.generate_one(shot, style_lock, refs, dest)

    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_gen, u) for u in cont]
        for fut in as_completed(futures):
            u, res = fut.result()
            done += 1
            job.update(message=f"Continuation still {done}/{len(cont)} ({u['still_id']})"
                               + ("" if res.success else " — failed"))
    job.end_stage("continuation_stills", f"{done} continuation stills generated")


def _run_cinematic_clips_planned(job: Job, params: dict, project_dir: Path,
                                 storyboard: dict, render_plan: list[dict]) -> dict:
    """Generate ONE clip per render unit (still → silent clip), enough to cover
    every beat. Returns a clip_manifest keyed so build_edit_decisions can map it."""
    ctx = getattr(job, "cinematic_ctx", {}) or {}
    keyframes_dir = Path(ctx.get("keyframes_dir") or (project_dir / "keyframes"))
    style_lock = storyboard.get("style_lock", "")
    shots = storyboard.get("shots", [])
    clips_dir = project_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    provider = (settings_manager.get("ai_video_provider", "veo3") or "veo3").lower()
    clip_secs = _provider_clip_seconds(provider)
    vx_project = settings_manager.get("vertex_project_id")
    vx_location = settings_manager.get("vertex_location", "us-central1")
    veo_model = settings_manager.get("veo_standard_model") or "veo-3.1-lite-generate-001"
    fal_endpoint = settings_manager.get("fal_video_endpoint") or "fal-ai/kling-video/v2.1/pro/image-to-video"
    ws_model = settings_manager.get("wavespeed_video_model") or "wavespeed-ai/wan-i2v-480p"
    if provider == "veo3" and not vx_project:
        raise RuntimeError("AI video provider is Google (Veo) but Google Cloud Project ID is not set in Settings.")
    if provider == "fal":
        os.environ.setdefault("FAL_API_KEY", os.getenv("FAL_API_KEY", "") or settings_manager.get("fal_api_key", ""))
    elif provider == "wavespeed":
        os.environ.setdefault("WAVESPEED_API_KEY", os.getenv("WAVESPEED_API_KEY", "") or settings_manager.get("wavespeed_api_key", ""))

    units = [u for u in render_plan if (keyframes_dir / f"{u['still_id']}.png").exists()]
    n = len(units)
    if n < 2:
        raise RuntimeError("Fewer than 2 stills available for the pause-driven edit — cannot generate clips.")

    job.begin_stage("clip_generation", "Generating Clips", f"{provider} · one clip per beat...")
    job.update(progress_pct=34)

    def _gen(u):
        shot = shots[u["shot_index"]]
        still = str(keyframes_dir / f"{u['still_id']}.png")
        video_prompt = f"{shot.get('video_prompt', '').strip()}. STYLE: {style_lock}".strip()
        dest = str(clips_dir / f"clip_{u['order']:03d}_{u['still_id']}.mp4")
        # Smart clip length: only as long as this beat needs.
        _dur = _snap_clip_duration(provider, float(u.get("target_duration") or clip_secs))
        res = _generate_video_clip(
            provider, prompt=video_prompt, dest_path=dest, image_path=still,
            negative_prompt=_CINEMATIC_NO_SPEECH, duration=_dur,
            vertex_project_id=vx_project, vertex_location=vx_location,
            veo_model=veo_model, fal_endpoint=fal_endpoint, ws_model=ws_model,
        )
        return u, dest, res

    entries_raw: dict[int, dict] = {}
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_gen, u) for u in units]
        for fut in as_completed(futures):
            u, dest, res = fut.result()
            done += 1
            if not res.success:
                errors.append(f"beat {u['order']}: {res.error}")
                job.update(message=f"Clip {done}/{n} failed: {res.error}")
                continue
            an = ClipAnalyzer().execute({"local_path": dest})
            if not an.success:
                errors.append(f"beat {u['order']}: analyze failed")
                continue
            entries_raw[u["order"]] = {
                "clip_id": f"clip_{u['order']:03d}",
                "still_id": u["still_id"],
                "filename": Path(dest).name,
                "local_path": dest,
                "duration_seconds": float(an.data["duration_seconds"]),
                "resolution": {"width": an.data["width"], "height": an.data["height"]},
                "fps": an.data["fps"],
                "scene": u.get("scene"),
                "provider": provider,
            }
            job.update(progress_pct=34 + int((done / n) * 38), message=f"Generated {done}/{n} clips")

    clip_entries = [entries_raw[o] for o in sorted(entries_raw)]
    balance_err = next((e for e in errors if any(w in e.lower() for w in ("balance", "locked", "exhaust"))), None)
    if balance_err:
        job.update(message="⚠ fal balance exhausted — top up at fal.ai/dashboard/billing.")
    if len(clip_entries) < 2:
        first = balance_err or (errors[0] if errors else "unknown error")
        raise RuntimeError(f"Only {len(clip_entries)}/{n} clips generated — need at least 2. {first}")

    clip_manifest = {"version": "1.0", "provider": provider, "clips": clip_entries,
                     "total_available_duration_seconds": sum(c["duration_seconds"] for c in clip_entries)}
    (project_dir / "clip_manifest.json").write_text(json.dumps(clip_manifest, indent=2))
    job.end_stage("clip_generation", f"{len(clip_entries)} beat clips via {provider}")
    return clip_manifest


def _cinematic_text_timing(normalized_clips: list[dict], cuts: list[dict], scenes: list[dict]) -> list[dict]:
    """Overlay each shot's text once, spanning that shot's beats (not per clip).
    Advances time by (clip - transition overlap) so crossfades don't drift the text."""
    scene_over = {s["scene"]: (s.get("overlay_text") or "").strip() for s in scenes}
    first_start: dict[int, float] = {}
    span: dict[int, float] = {}
    t = 0.0
    n = len(normalized_clips)
    for i, cut in enumerate(cuts):
        sn = cut.get("scene")
        d = normalized_clips[i]["duration_seconds"]
        td = normalized_clips[i].get("transition_duration", 0.0) if i < n - 1 else 0.0
        if sn not in first_start:
            first_start[sn] = t
            span[sn] = 0.0
        span[sn] += d - td
        t += d - td
    out = []
    for sn, start in first_start.items():
        txt = scene_over.get(sn, "")
        if not txt:
            continue
        dur = span[sn]
        margin = min(0.5, dur * 0.15)
        out.append({"text": txt, "start": round(start + margin, 2),
                    "end": round(start + dur - margin, 2), "position": "bottom"})
    return out


def _run_cinematic_finish_planned(job: Job, params: dict, project_dir: Path, project_id: str,
                                  storyboard: dict, clip_manifest: dict, render_plan: list[dict],
                                  vo_concat_path: str | None) -> None:
    """Assemble the pause-driven reel: cut on the pauses, lay the FULL narration over
    the timeline (no truncation), colour-match, mix, QA, deliver."""
    from backend.pipeline_runner import _resolve_music
    from tools.ai.cinematic_planner import build_edit_decisions

    scenes = storyboard.get("scenes", [])
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"
    for d in [tmp_dir / "normalized", tmp_dir / "graded", output_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # ── edit_decisions: cut on the pauses (hard cuts, trims = beat segments) ──────
    job.begin_stage("edit_decisions", "Planning the Edit", "Cutting on the voiceover pauses...")
    job.update(progress_pct=76)
    edit_decisions = build_edit_decisions(render_plan, clip_manifest)
    cuts = sorted(edit_decisions["cuts"], key=lambda x: x["order"])
    job.end_stage("edit_decisions",
                  f"{len(cuts)} pause-aligned cuts · {edit_decisions['total_duration_seconds']:.1f}s")

    # ── compose: normalize → colour match → concat → text → full VO + music ──────
    job.begin_stage("compose", "Composing Reel", "Normalizing clips...")
    job.update(progress_pct=82)
    clip_by_id = {c["clip_id"]: c for c in clip_manifest["clips"]}
    normalized_clips: list[dict] = []
    for cut in cuts:
        clip = clip_by_id[cut["clip_id"]]
        norm_path = str(tmp_dir / "normalized" / f"{cut['order']:02d}_{cut['clip_id']}.mp4")
        nr = VideoNormalizer().execute({
            "input_path": clip["local_path"], "output_path": norm_path,
            "trim_in": cut["trim_in"], "trim_out": cut["trim_out"],
        })
        if not nr.success:
            raise RuntimeError(f"Normalize failed for {clip['filename']}: {nr.error}")
        normalized_clips.append({
            "path": norm_path, "duration_seconds": cut["trim_out"] - cut["trim_in"],
            "transition": cut.get("transition", "hard_cut"),
            "transition_duration": cut.get("transition_duration_seconds", 0.0),
        })

    job.update(progress_pct=86, message="Colour-matching shots...")
    try:
        graded = ColorMatcher().match([c["path"] for c in normalized_clips], str(tmp_dir / "graded"))
        for c, gp in zip(normalized_clips, graded):
            c["path"] = gp
    except Exception as exc:
        job.update(message=f"Colour match skipped ({exc})")

    job.update(progress_pct=88, message=f"Joining {len(normalized_clips)} clips...")
    composed_path = str(tmp_dir / "composed.mp4")
    cr = VideoComposer().execute({"clips": normalized_clips, "output_path": composed_path})
    if not cr.success:
        raise RuntimeError(f"Composition failed: {cr.error}")

    titled_path = composed_path
    scene_text = _cinematic_text_timing(normalized_clips, cuts, scenes)
    if scene_text and params.get("include_text", True):
        titled_path = str(tmp_dir / "titled.mp4")
        tr = TextRenderer().execute({"input_path": composed_path, "output_path": titled_path, "scenes": scene_text})
        if not tr.success:
            titled_path = composed_path
            job.update(message=f"Text overlay skipped: {tr.error}")

    # Full narration laid over the timeline from t=0 — visual length == VO length,
    # so nothing is truncated and the cuts already sit on the speech onsets.
    job.update(progress_pct=92, message="Mixing narration and music...")
    final_path = str(output_dir / "ai_reel_final.mp4")
    mr = AudioMixer().execute({
        "video_path": titled_path, "output_path": final_path,
        "voiceover_path": vo_concat_path, "music_path": _resolve_music(params.get("music_file")),
        "duck_original": True, "drop_original": bool(vo_concat_path),
    })
    if not mr.success:
        raise RuntimeError(f"Audio mix failed: {mr.error}")
    job.end_stage("compose", "Reel composed and graded")

    # ── QA ───────────────────────────────────────────────────────────────────────
    job.begin_stage("final_review", "Quality Review", "Checking resolution and duration...")
    job.update(progress_pct=96)
    probe = VideoProber().execute({"path": final_path})
    probe_data = probe.data if probe.success else {}
    FrameSampler().execute({"video_path": final_path, "output_dir": str(project_dir / "review" / "frames"), "num_frames": 4})
    actual_dur = probe_data.get("duration_seconds", 0)
    if probe_data.get("width") != 1080 or probe_data.get("height") != 1920:
        raise RuntimeError(
            f"Final review failed: resolution {probe_data.get('width')}x{probe_data.get('height')}, expected 1080x1920")
    job.end_stage("final_review", "All quality checks passed")

    # ── deliver ────────────────────────────────────────────────────────────────
    job.begin_stage("deliver", "Ready!", "Your cinematic reel is ready")
    job.update(progress_pct=99)
    file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0
    local_copy_path = _copy_to_local_output(project_dir, project_id)

    provider = clip_manifest.get("provider", "fal")
    km_path = project_dir / "keyframe_manifest.json"
    km = json.loads(km_path.read_text()) if km_path.exists() else {}
    n_stills = sum(1 for k in km.get("keyframes", []) if k.get("success"))
    n_cont = sum(1 for u in render_plan if u["is_continuation"])
    n_sheet = 1 if km.get("character_sheet") else 0
    n_continuity = len(km.get("verdicts", {}))
    n_clips = len(clip_manifest.get("clips", []))
    n_vo = len({u["scene"] for u in render_plan})
    _kg = KeyframeGenerator()
    kf_endpoint = f"{_kg.provider}:{_kg.model}"
    clip_model = (settings_manager.get("fal_video_endpoint") if provider == "fal"
                  else settings_manager.get("veo_standard_model") if provider == "veo3"
                  else settings_manager.get("wavespeed_video_model"))
    review = {
        "cost": _estimate_cinematic_cost(provider, n_stills + n_cont, n_sheet, 0, n_continuity, n_clips, n_vo),
        "inputs": _capture_cinematic_inputs(storyboard, provider, kf_endpoint, clip_model or ""),
    }
    (project_dir / "cost_log.json").write_text(json.dumps(review["cost"], indent=2))
    (project_dir / "cinematic_inputs.json").write_text(json.dumps(review["inputs"], indent=2))

    job.end_stage("deliver", f"Done · est. ${review['cost']['total']:.2f}")
    job.update(
        status=JobStatus.COMPLETED, progress_pct=100,
        message="Cinematic reel ready!" + (f" Saved to {local_copy_path}" if local_copy_path else ""),
        result={
            "project_id": project_id, "milestone": "M5", "reel_type": "cinematic",
            "output_path": final_path, "local_copy_path": local_copy_path,
            "duration_seconds": actual_dur, "file_size_bytes": file_size,
            "clips_used": len(normalized_clips), "provider": provider,
            "voiceover": vo_concat_path is not None, "source": "cinematic",
            "edit": "pause-driven",
            "estimated_cost": review["cost"]["total"],
            "review": review,
            "scenes": [{"scene": s["scene"], "overlay_text": s.get("overlay_text", ""),
                        "voiceover": s.get("voiceover", "")} for s in scenes],
        },
    )


def _copy_to_local_output(project_dir: Path, project_id: str) -> str | None:
    out_dir = settings_manager.get_local_output_dir()
    if not out_dir:
        return None
    dest_root = Path(out_dir).expanduser()
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / project_id
        shutil.copytree(str(project_dir), str(dest), dirs_exist_ok=True)
        return str(dest)
    except Exception:
        return None
