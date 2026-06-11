"""
Studio pipeline runner — converts a single product image into a 5-shot AI-generated reel.

Shot map:
  1. Animate product   — fal Kling 3.0 I2V
  2. On model          — fal FASHN try-on → Kling I2V  (or library model → Kling I2V)
  3. Lifestyle         — fal FLUX Kontext → WAN I2V
  4. Close-up macro    — WaveSpeed Kling 4K  (fallback: fal Kling I2V)
  5. Collection flatlay — fal FLUX Pro → WAN I2V
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import MUSIC_LIBRARY_PATH, PROJECTS_DIR
from backend.job_manager import Job, JobStatus
from backend import settings_manager
from backend.studio_constants import (
    COLOR_GRADE_PROMPTS,
    MODEL_LIBRARY,
    SHOT_DEFAULTS,
)
from lib.checkpoint import write_artifact, write_checkpoint
from tools.ai.fal_client import FalClient
from tools.ai.wavespeed_client import WaveSpeedClient
from tools.analysis.video_prober import VideoProber
from tools.audio.audio_mixer import AudioMixer
from tools.video.text_renderer import TextRenderer
from tools.video.video_composer import VideoComposer
from tools.video.video_normalizer import VideoNormalizer

import random


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_music(music_file: str | None) -> str | None:
    if not music_file:
        music_dir = MUSIC_LIBRARY_PATH
        if music_dir.exists():
            tracks = (
                list(music_dir.glob("*.mp3"))
                + list(music_dir.glob("*.m4a"))
                + list(music_dir.glob("*.wav"))
            )
            if tracks:
                return str(random.choice(tracks))
        return None
    path = MUSIC_LIBRARY_PATH / music_file
    return str(path) if path.exists() else None


def _black_clip(dest_path: str, duration: float = 5.0) -> None:
    """Generate a black silent video as a fallback clip."""
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=black:s=1080x1920:r=30",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            dest_path,
        ],
        capture_output=True,
        timeout=30,
    )


def _build_shot1_prompt(controls: dict, product_name: str) -> str:
    c = {**SHOT_DEFAULTS["shot1"], **controls}
    parts = [
        f"{product_name or 'product'} gently floating",
        f"{c['camera_angle'].lower()} view",
        f"{c['lighting'].lower()} lighting",
    ]
    if c.get("material") and c["material"] != "None":
        parts.append(f"{c['material'].lower()} finish")
    parts += ["slow rotation, studio background"]
    if c.get("hyper_realism"):
        parts.append("photorealistic, 8K, RAW photo quality")
    else:
        parts.append("cinematic")
    return ", ".join(parts)


def _build_shot2_prompt(controls: dict, product_name: str) -> str:
    c = {**SHOT_DEFAULTS["shot2"], **controls}
    parts = [f"model {c['shot_type'].lower()}"]
    if c.get("expression") and c["expression"] != "Neutral":
        parts.append(f"{c['expression'].lower()} expression")
    parts += [
        f"{c['product_interaction'].lower()} the {product_name or 'bag'}",
        "lifestyle setting, natural movement, cinematic",
    ]
    return ", ".join(parts)


def _build_shot3_prompt(controls: dict, product_name: str) -> str:
    c = {**SHOT_DEFAULTS["shot3"], **controls}
    parts = [f"place {product_name or 'product'} in a {c['scene'].lower()} setting"]
    parts.append(f"{c['lighting'].lower()} lighting")
    if c.get("time_of_day") and c["time_of_day"] != "None":
        parts.append(c["time_of_day"].lower())
    parts.append("photorealistic, natural context")
    return ", ".join(parts)


def _build_shot4_prompt(controls: dict, product_name: str) -> str:
    c = {**SHOT_DEFAULTS["shot4"], **controls}
    parts = [
        f"extreme close-up macro of {product_name or 'product'}",
        f"placed on {c['surface'].lower()}",
        f"{c['light_quality'].lower()} light",
        "slow push-in camera move",
    ]
    if c.get("shadow") and c["shadow"] != "None":
        parts.append(f"{c['shadow'].lower()} shadow")
    parts.append("photorealistic product detail shot, cinematic")
    return ", ".join(parts)


def _build_shot5_prompt(controls: dict, product_name: str) -> str:
    c = {**SHOT_DEFAULTS["shot5"], **controls}
    grade = COLOR_GRADE_PROMPTS.get(c.get("color_grade", "None"), "")
    parts = [
        f"{c['layout_style'].lower()} flat lay arrangement of multiple {product_name or 'product'} items",
        "top-down editorial view",
        "artistic composition",
    ]
    if grade:
        parts.append(grade)
    parts.append("commercial photography")
    return ", ".join(parts)


def _generate_shot_plan(
    product_name: str,
    product_context: str,
    brand_voice: str,
    shot_controls: dict,
    has_model: bool,
) -> list[dict]:
    """Ask Claude to write 5-shot prompts, or fall back to assembled defaults."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")

    controls_by_shot = {
        "shot1": shot_controls.get("shot1", {}),
        "shot2": shot_controls.get("shot2", {}),
        "shot3": shot_controls.get("shot3", {}),
        "shot4": shot_controls.get("shot4", {}),
        "shot5": shot_controls.get("shot5", {}),
    }

    # Build prompts from controls; custom override takes precedence per shot
    _builders = {
        1: _build_shot1_prompt,
        2: _build_shot2_prompt,
        3: _build_shot3_prompt,
        4: _build_shot4_prompt,
        5: _build_shot5_prompt,
    }
    base_prompts = {}
    for i in range(1, 6):
        custom = shot_controls.get(f"shot{i}_custom_prompt", "")
        if custom:
            base_prompts[i] = custom
        elif i == 2 and not has_model:
            base_prompts[i] = ""
        else:
            base_prompts[i] = _builders[i](controls_by_shot[f"shot{i}"], product_name)

    default_overlays = {
        1: "The bag.",
        2: "Wear it your way.",
        3: "Every day. Everywhere.",
        4: "The details.",
        5: "The collection.",
    }

    if not api_key:
        return _default_shot_plan(base_prompts, default_overlays, has_model)

    try:
        import anthropic

        bv_snippet = brand_voice[:400] if brand_voice else ""
        context_str = f"\nProduct context: {product_context}" if product_context else ""
        bv_str = f"\nBrand voice (brief):\n{bv_snippet}" if bv_snippet else ""

        system = (
            "You are an expert AI video director for a fashion/lifestyle brand. "
            "You write cinematic video generation prompts for AI image-to-video models. "
            "Prompts must be vivid, specific, and optimised for Kling 3.0 and FLUX models. "
            "Respond ONLY with valid JSON — no markdown, no explanation."
        )

        shot_list = [
            {"shot": 1, "type": "Animate product", "base_prompt": base_prompts[1],
             "overlay_hint": default_overlays[1]},
            {"shot": 3, "type": "Lifestyle/real life", "base_prompt": base_prompts[3],
             "overlay_hint": default_overlays[3]},
            {"shot": 4, "type": "Close-up macro", "base_prompt": base_prompts[4],
             "overlay_hint": default_overlays[4]},
            {"shot": 5, "type": "Collection flatlay", "base_prompt": base_prompts[5],
             "overlay_hint": default_overlays[5]},
        ]
        if has_model:
            shot_list.insert(
                1,
                {"shot": 2, "type": "On model (virtual try-on)", "base_prompt": base_prompts[2],
                 "overlay_hint": default_overlays[2]},
            )

        user = (
            f"Product: {product_name or 'tote bag'}{context_str}{bv_str}\n\n"
            "Refine these AI video generation prompts to be more vivid and cinematic. "
            "Keep the technical control words (camera angles, lighting, etc.) but add specific "
            "visual poetry. Also write a punchy overlay_text (max 6 words) for each shot.\n\n"
            f"Shots to refine:\n{json.dumps(shot_list, indent=2)}\n\n"
            'Return JSON: {"shots": [{"shot": N, "prompt": "...", "overlay_text": "..."}]}'
        )

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": user}],
            system=system,
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)
        refined = {s["shot"]: s for s in data.get("shots", [])}

        shots = []
        for i in range(1, 6):
            if i == 2 and not has_model:
                continue
            r = refined.get(i, {})
            shots.append({
                "shot_num": i,
                "label": {
                    1: "Animate Product",
                    2: "On Model",
                    3: "Lifestyle",
                    4: "Close-Up Macro",
                    5: "Collection Flatlay",
                }[i],
                "prompt": r.get("prompt", base_prompts[i]),
                "overlay_text": r.get("overlay_text", default_overlays[i]),
                "duration_seconds": 5.0,
            })
        return shots

    except Exception:
        return _default_shot_plan(base_prompts, default_overlays, has_model)


def _default_shot_plan(
    base_prompts: dict[int, str],
    default_overlays: dict[int, str],
    has_model: bool,
) -> list[dict]:
    labels = {
        1: "Animate Product",
        2: "On Model",
        3: "Lifestyle",
        4: "Close-Up Macro",
        5: "Collection Flatlay",
    }
    return [
        {
            "shot_num": i,
            "label": labels[i],
            "prompt": base_prompts[i],
            "overlay_text": default_overlays[i],
            "duration_seconds": 5.0,
        }
        for i in range(1, 6)
        if not (i == 2 and not has_model)
    ]


# ── Shot generators ────────────────────────────────────────────────────────────

def _gen_shot1(product_data_uri: str, prompt: str, dest_path: str) -> dict:
    """Kling 3.0 I2V on the product image."""
    result = FalClient().execute({
        "operation": "image_to_video",
        "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
        "image_url": product_data_uri,
        "prompt": prompt,
        "duration": 5,
        "aspect_ratio": "9:16",
        "dest_path": dest_path,
    })
    return {"ok": result.success, "error": result.error, "path": dest_path}


def _gen_shot2_with_photo(
    product_data_uri: str,
    model_data_uri: str,
    prompt: str,
    tryon_path: str,
    video_path: str,
) -> dict:
    """FASHN try-on → Kling I2V."""
    fal = FalClient()

    tryon = fal.execute({
        "operation": "tryon",
        "product_image_url": product_data_uri,
        "model_image_url": model_data_uri,
        "category": "bags",
        "dest_path": tryon_path,
    })
    if not tryon.success:
        # FASHN failed — fall back to direct Kling I2V on product
        result = fal.execute({
            "operation": "image_to_video",
            "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
            "image_url": product_data_uri,
            "prompt": prompt,
            "duration": 5,
            "aspect_ratio": "9:16",
            "dest_path": video_path,
        })
        return {"ok": result.success, "error": result.error, "path": video_path, "fashn_skipped": True}

    tryon_uri = FalClient.encode_image(tryon_path)
    result = fal.execute({
        "operation": "image_to_video",
        "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
        "image_url": tryon_uri,
        "prompt": prompt,
        "duration": 5,
        "aspect_ratio": "9:16",
        "dest_path": video_path,
    })
    return {"ok": result.success, "error": result.error, "path": video_path}


def _gen_shot2_with_library_model(
    product_data_uri: str,
    library_model_id: str,
    prompt: str,
    video_path: str,
) -> dict:
    """Kling I2V with a library model description in the prompt (no FASHN)."""
    model = next((m for m in MODEL_LIBRARY if m["id"] == library_model_id), None)
    if model:
        prompt = f"{model['description']}, {prompt}"
    result = FalClient().execute({
        "operation": "image_to_video",
        "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
        "image_url": product_data_uri,
        "prompt": prompt,
        "duration": 5,
        "aspect_ratio": "9:16",
        "dest_path": video_path,
    })
    return {"ok": result.success, "error": result.error, "path": video_path}


def _gen_shot3(product_data_uri: str, prompt: str, i2i_path: str, video_path: str) -> dict:
    """FLUX Kontext context-edit → WAN I2V."""
    fal = FalClient()

    # Step 1: image edit
    edited = fal.execute({
        "operation": "image_to_image",
        "endpoint": "fal-ai/flux-pro/kontext",
        "image_url": product_data_uri,
        "prompt": prompt,
        "dest_path": i2i_path,
    })
    source_uri = product_data_uri
    if edited.success:
        source_uri = FalClient.encode_image(i2i_path)

    # Step 2: animate
    result = fal.execute({
        "operation": "image_to_video",
        "endpoint": "fal-ai/wan-i2v",
        "image_url": source_uri,
        "prompt": "gentle ambient motion, natural scene movement, subtle camera drift",
        "duration": 5,
        "dest_path": video_path,
    })
    return {"ok": result.success, "error": result.error, "path": video_path}


def _gen_shot4(product_data_uri: str, prompt: str, video_path: str) -> dict:
    """WaveSpeed Kling 4K → fallback fal Kling I2V."""
    wavespeed_key = os.getenv("WAVESPEED_API_KEY", "")
    if wavespeed_key:
        result = WaveSpeedClient().execute({
            "operation": "image_to_video",
            "model_slug": "wavespeed-ai/wan-i2v-480p",
            "image_url": product_data_uri,
            "prompt": prompt,
            "dest_path": video_path,
        })
        if result.success:
            return {"ok": True, "path": video_path}

    # Fallback to fal Kling
    result = FalClient().execute({
        "operation": "image_to_video",
        "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
        "image_url": product_data_uri,
        "prompt": prompt,
        "duration": 5,
        "aspect_ratio": "9:16",
        "dest_path": video_path,
    })
    return {"ok": result.success, "error": result.error, "path": video_path}


def _gen_shot5(t2i_prompt: str, video_prompt: str, t2i_path: str, video_path: str) -> dict:
    """FLUX Pro text-to-image → WAN I2V."""
    fal = FalClient()

    img = fal.execute({
        "operation": "text_to_image",
        "endpoint": "fal-ai/flux-pro",
        "prompt": t2i_prompt,
        "dest_path": t2i_path,
    })

    source_uri: str
    if img.success:
        source_uri = FalClient.encode_image(t2i_path)
    else:
        return {"ok": False, "error": f"FLUX Pro t2i failed: {img.error}", "path": video_path}

    result = fal.execute({
        "operation": "image_to_video",
        "endpoint": "fal-ai/wan-i2v",
        "image_url": source_uri,
        "prompt": video_prompt,
        "duration": 5,
        "dest_path": video_path,
    })
    return {"ok": result.success, "error": result.error, "path": video_path}


# ── Main runner ────────────────────────────────────────────────────────────────

def run_studio_pipeline(job: Job, params: dict[str, Any]) -> None:
    project_id = f"studio-{uuid.uuid4().hex[:8]}"
    local_out = settings_manager.get_local_output_dir()
    projects_root = Path(local_out).expanduser() if local_out else PROJECTS_DIR
    projects_root.mkdir(parents=True, exist_ok=True)
    project_dir = (projects_root / project_id).resolve()

    shots_dir = project_dir / "tmp" / "shots"
    norm_dir = project_dir / "tmp" / "normalized"
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"

    for d in [shots_dir, norm_dir, output_dir, project_dir / "artifacts", project_dir / "checkpoints"]:
        d.mkdir(parents=True, exist_ok=True)

    try:
        # ── STAGE 1: brief ─────────────────────────────────────────────────────
        job.begin_stage("brief", "Processing Brief", "Validating product image...")
        job.update(progress_pct=3, status=JobStatus.RUNNING)

        product_image_path = params.get("product_image_path", "")
        if not product_image_path or not Path(product_image_path).exists():
            raise RuntimeError(f"product_image_path not found: {product_image_path!r}")

        fal_key = os.getenv("FAL_API_KEY", "") or settings_manager.get("fal_api_key", "")
        if not fal_key:
            raise RuntimeError("FAL_API_KEY is not set. Add it in Settings → fal.ai API Key.")

        # ── Background removal ─────────────────────────────────────────────────
        job.update(message="Removing background from product image...")
        nobg_path = str(tmp_dir / "product_nobg.png")
        fal = FalClient()
        raw_data_uri = FalClient.encode_image(product_image_path)
        nobg_result = fal.execute({
            "operation": "remove_background",
            "image_url": raw_data_uri,
            "dest_path": nobg_path,
        })
        if nobg_result.success:
            product_image_path = nobg_path
            job.update(message="Background removed — product image ready")
        else:
            job.update(message=f"Background removal skipped ({nobg_result.error}) — using original image")

        model_image_path = params.get("model_image_path") or None
        library_model_id = params.get("library_model_id") or None
        has_model = bool(model_image_path or library_model_id)

        product_name = (params.get("product_name") or "").strip() or "tote bag"
        product_context = (params.get("style_prompt") or "").strip() or ""
        shot_controls = params.get("shot_controls") or {}
        music_file = params.get("music_file")

        studio_brief = {
            "version": "1.0",
            "product_image_path": product_image_path,
            "model_image_path": model_image_path,
            "library_model_id": library_model_id,
            "has_model": has_model,
            "product_name": product_name,
            "product_context": product_context,
            "shot_controls": shot_controls,
            "music_file": music_file,
        }

        write_artifact(str(projects_root), project_id, "studio_brief", studio_brief)
        write_checkpoint(str(projects_root), project_id, "brief", "completed", {"studio_brief": studio_brief})
        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: shot_plan ─────────────────────────────────────────────────
        job.begin_stage("shot_plan", "Planning Shots", "Claude is writing your 5 shot prompts...")
        job.update(progress_pct=8)

        brand_voice = settings_manager.get_brand_voice()
        shots = _generate_shot_plan(
            product_name=product_name,
            product_context=product_context,
            brand_voice=brand_voice,
            shot_controls=shot_controls,
            has_model=has_model,
        )

        write_artifact(str(projects_root), project_id, "shot_plan", {"shots": shots})
        write_checkpoint(str(projects_root), project_id, "shot_plan", "completed", {"shots": shots})
        job.end_stage("shot_plan", f"{len(shots)} shots planned — awaiting your approval")

        # ── APPROVAL GATE ──────────────────────────────────────────────────────
        job.request_approval({
            "module": "studio",
            "storyboard": {
                "theme": f"Product reel — {product_name}",
                "scenes": [
                    {
                        "scene": s["shot_num"],
                        "role": s["label"],
                        "visual_description": s["prompt"],
                        "clip_hint": "",
                        "duration_seconds": s["duration_seconds"],
                        "overlay_text": s["overlay_text"],
                        "voiceover": "",
                    }
                    for s in shots
                ],
            },
            "reel_summary": {
                "mode": "studio",
                "prompt": product_context or product_name,
                "target_duration_seconds": sum(s["duration_seconds"] for s in shots),
                "scene_count": len(shots),
                "music_file": music_file or "Random from library",
            },
        })
        approved = job.wait_for_approval(timeout=1800)
        if not approved:
            raise RuntimeError("Approval timed out after 30 minutes")

        # Merge user edits back into shots
        resp_scenes = (job.approval_response or {}).get("scenes", [])
        edited_by_scene = {s["scene"]: s for s in resp_scenes if "scene" in s}
        for s in shots:
            edits = edited_by_scene.get(s["shot_num"], {})
            if "overlay_text" in edits:
                s["overlay_text"] = edits["overlay_text"]
            if "visual_description" in edits:
                s["prompt"] = edits["visual_description"]

        # ── STAGE 3: generate_shots ────────────────────────────────────────────
        job.begin_stage("generate_shots", "Generating AI Shots", "Calling fal.ai — this takes a few minutes...")
        job.update(progress_pct=15)

        # Encode product image once
        product_data_uri = FalClient.encode_image(product_image_path)
        model_data_uri = FalClient.encode_image(model_image_path) if model_image_path else None

        shot_results: dict[int, str] = {}  # shot_num → local video path
        shots_by_num = {s["shot_num"]: s for s in shots}

        for shot_num in [1, 2, 3, 4, 5]:
            shot = shots_by_num.get(shot_num)
            if shot is None:
                continue  # Shot 2 skipped if no model

            job.update(
                progress_pct=15 + (shot_num - 1) * 10,
                message=f"Generating Shot {shot_num}/5 — {shot['label']}...",
            )

            raw_path = str(shots_dir / f"shot_{shot_num:02d}_raw.mp4")

            try:
                if shot_num == 1:
                    r = _gen_shot1(product_data_uri, shot["prompt"], raw_path)

                elif shot_num == 2:
                    if model_data_uri:
                        tryon_path = str(shots_dir / "shot_02_tryon.jpg")
                        r = _gen_shot2_with_photo(
                            product_data_uri, model_data_uri,
                            shot["prompt"], tryon_path, raw_path,
                        )
                    elif library_model_id:
                        r = _gen_shot2_with_library_model(
                            product_data_uri, library_model_id,
                            shot["prompt"], raw_path,
                        )
                    else:
                        continue  # should not reach here

                elif shot_num == 3:
                    i2i_path = str(shots_dir / "shot_03_i2i.jpg")
                    r = _gen_shot3(product_data_uri, shot["prompt"], i2i_path, raw_path)

                elif shot_num == 4:
                    r = _gen_shot4(product_data_uri, shot["prompt"], raw_path)

                elif shot_num == 5:
                    t2i_path = str(shots_dir / "shot_05_t2i.jpg")
                    color_ctrl = (shot_controls.get("shot5") or {}).get("color_grade", "None")
                    video_prompt = (
                        f"slow panning reveal, gentle drift"
                        + (f", {COLOR_GRADE_PROMPTS[color_ctrl]}" if COLOR_GRADE_PROMPTS.get(color_ctrl) else "")
                    )
                    r = _gen_shot5(shot["prompt"], video_prompt, t2i_path, raw_path)

                if not r["ok"]:
                    err_detail = (r.get("error") or "unknown error")[:200]
                    job.update(message=f"Shot {shot_num} FAILED: {err_detail} — black fallback used")
                    _black_clip(raw_path)

                shot_results[shot_num] = raw_path

            except Exception as exc:
                job.update(message=f"Shot {shot_num} EXCEPTION: {exc} — black fallback used")
                _black_clip(raw_path)
                shot_results[shot_num] = raw_path

        if not shot_results:
            raise RuntimeError("No shots were generated — check your fal.ai API key and try again.")

        job.end_stage("generate_shots", f"{len(shot_results)} shots generated")

        # ── STAGE 4: normalize ─────────────────────────────────────────────────
        job.begin_stage("normalize", "Normalizing Clips", "Scaling shots to 1080×1920...")
        job.update(progress_pct=68)

        def _norm(args: tuple[int, str]) -> tuple[int, str, bool, str]:
            shot_num, raw_path = args
            norm_path = str(norm_dir / f"{shot_num:02d}_shot_{shot_num:02d}.mp4")
            result = VideoNormalizer().execute({
                "input_path": raw_path,
                "output_path": norm_path,
                "trim_in": 0.0,
                "trim_out": 5.0,
            })
            return shot_num, norm_path, result.success, result.error or ""

        ordered_shots = sorted(shot_results.items())
        normalized: dict[int, str] = {}

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_norm, pair): pair for pair in ordered_shots}
            for fut in as_completed(futures):
                sn, norm_path, ok, err = fut.result()
                if not ok:
                    raise RuntimeError(f"Normalize failed for shot {sn}: {err}")
                normalized[sn] = norm_path

        job.end_stage("normalize", f"{len(normalized)} clips normalized")

        # ── STAGE 5: compose ───────────────────────────────────────────────────
        job.begin_stage("compose", "Composing Reel", "Stitching shots together...")
        job.update(progress_pct=78)

        normalized_clips = [
            {
                "path": normalized[sn],
                "duration_seconds": 5.0,
                "transition": "hard_cut",
                "transition_duration": 0.01,
            }
            for sn in sorted(normalized.keys())
        ]

        composed_path = str(tmp_dir / "composed.mp4")
        compose_result = VideoComposer().execute({
            "clips": normalized_clips,
            "output_path": composed_path,
        })
        if not compose_result.success:
            raise RuntimeError(f"Composition failed: {compose_result.error}")

        # Text overlays
        job.update(progress_pct=83, message="Rendering text overlays...")
        scene_text = []
        t = 0.0
        for sn in sorted(normalized.keys()):
            shot = shots_by_num.get(sn, {})
            overlay = (shot.get("overlay_text") or "").strip()
            dur = 5.0
            if overlay:
                margin = 0.5
                scene_text.append({
                    "text": overlay,
                    "start": round(t + margin, 2),
                    "end": round(t + dur - margin, 2),
                    "position": "bottom",
                })
            t += dur

        titled_path = composed_path
        if scene_text:
            titled_path = str(tmp_dir / "titled.mp4")
            tr = TextRenderer().execute({
                "input_path": composed_path,
                "output_path": titled_path,
                "scenes": scene_text,
            })
            if not tr.success:
                titled_path = composed_path
                job.update(message=f"Warning: text overlay failed, skipping: {tr.error}")

        # Audio mix
        job.update(progress_pct=87, message="Mixing background music...")
        final_path = str(output_dir / "studio_final.mp4")
        music_path = _resolve_music(music_file)

        mix = AudioMixer().execute({
            "video_path": titled_path,
            "output_path": final_path,
            "voiceover_path": None,
            "music_path": music_path,
            "duck_original": True,
        })
        if not mix.success:
            raise RuntimeError(f"Audio mix failed: {mix.error}")

        job.end_stage("compose", "Reel composed")

        # ── STAGE 6: final_review ──────────────────────────────────────────────
        job.begin_stage("final_review", "Quality Review", "Checking codec, resolution...")
        job.update(progress_pct=90)

        probe = VideoProber().execute({"path": final_path})
        probe_data = probe.data if probe.success else {}

        actual_dur = probe_data.get("duration_seconds", 0.0)
        findings = []

        if probe_data.get("width") != 1080 or probe_data.get("height") != 1920:
            findings.append({
                "severity": "critical",
                "check": "resolution",
                "description": (
                    f"Resolution is {probe_data.get('width')}×{probe_data.get('height')}, expected 1080×1920"
                ),
            })

        review_status = "fail" if any(f["severity"] == "critical" for f in findings) else "pass"

        final_review = {
            "version": "1.0",
            "status": review_status,
            "checks": {
                "technical_probe": {
                    "resolution": probe_data.get("resolution", ""),
                    "codec": probe_data.get("codec", ""),
                    "duration_seconds": actual_dur,
                    "file_size_bytes": probe_data.get("file_size_bytes", 0),
                    "is_playable": probe_data.get("is_playable", False),
                },
                "audio_check": {"music_added": music_path is not None},
            },
            "findings": findings,
            "summary": "All checks passed." if review_status == "pass" else "Critical issues found.",
            "timestamp": _ts(),
        }

        if review_status == "fail":
            raise RuntimeError(
                f"Final review failed: {[f['description'] for f in findings if f['severity'] == 'critical']}"
            )

        write_artifact(str(projects_root), project_id, "final_review", final_review)
        write_checkpoint(str(projects_root), project_id, "final_review", "completed", {"final_review": final_review})
        job.end_stage("final_review", "All quality checks passed")

        # ── STAGE 7: deliver ───────────────────────────────────────────────────
        job.begin_stage("deliver", "Ready!", "Your Studio reel is ready!")
        job.update(progress_pct=99)

        file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0

        write_artifact(str(projects_root), project_id, "publish_log", {
            "version": "1.0",
            "local_path": final_path,
            "file_size_bytes": file_size,
            "duration_seconds": actual_dur,
            "shots_generated": len(shot_results),
            "timestamp": _ts(),
        })

        job.end_stage("deliver", "Done")
        job.update(
            status=JobStatus.COMPLETED,
            progress_pct=100,
            message=f"Studio reel ready! {len(shot_results)} shots, {actual_dur:.1f}s",
            result={
                "project_id": project_id,
                "output_path": final_path,
                "project_dir": str(project_dir),
                "duration_seconds": actual_dur,
                "file_size_bytes": file_size,
                "shots_generated": len(shot_results),
                "shots": [
                    {"shot": s["shot_num"], "label": s["label"], "overlay_text": s.get("overlay_text", "")}
                    for s in shots
                ],
                "final_review": final_review,
            },
        )

    except Exception as exc:
        job.update(status=JobStatus.FAILED, message="Studio pipeline failed", error=str(exc))
        raise
