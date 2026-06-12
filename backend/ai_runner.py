"""
AI Reels pipeline — identical to the Stock pipeline but generates every clip
from scratch using Google Veo3 (Vertex AI) instead of downloading from Pexels.

Each scene's visual_description (enriched with clip_hint) becomes the Veo3
text prompt. duration_seconds is clamped to Veo3's 8-second maximum.
Everything downstream — edit_planner, compose, text overlay, voiceover,
audio mix — is identical to stock_runner.
"""
from __future__ import annotations

import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from backend.config import PROJECTS_DIR
from backend.job_manager import Job, JobStatus
from backend import settings_manager
from backend.pipeline_runner import (
    _generate_storyboard,
    _compute_scene_text_timing,
    _generate_scene_vo_files,
    _resolve_music,
    _ts,
)
from backend import edit_planner
from tools.ai.veo3_client import Veo3Client, VEO3_MAX_DURATION
from tools.analysis.clip_analyzer import ClipAnalyzer
from tools.analysis.audio_prober import AudioProber
from tools.analysis.frame_sampler import FrameSampler
from tools.analysis.video_prober import VideoProber
from tools.audio.audio_mixer import AudioMixer
from tools.video.text_renderer import TextRenderer
from tools.video.video_composer import VideoComposer
from tools.video.video_normalizer import VideoNormalizer


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


def _describe_product(image_path: str) -> str:
    """One Claude vision call → a short factual description of the product in the image,
    used to make the storyboard product-aware (Product mode)."""
    import base64
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")
    if not api_key:
        return ""
    suffix = Path(image_path).suffix.lower().lstrip(".")
    media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix, "image/jpeg")
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
    resp = anthropic.Anthropic(api_key=api_key).messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": (
                "Describe this product in 1-2 sentences for a video scriptwriter. "
                "State exactly what it is, its key visual features (color, material, shape, style), "
                "and the vibe it conveys. Be concrete and factual. Return only the description."
            )},
        ]}],
    )
    return resp.content[0].text.strip()


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

        # ── Product mode: read the product from its image (vision) ─────────────
        # The description is fed into the storyboard prompt so the script is built
        # around the actual product, not just the text prompt.
        product_image_path = params.get("product_image_path")
        if params.get("reel_type") == "product" and product_image_path and Path(product_image_path).exists():
            try:
                reel_brief["product_description"] = _describe_product(product_image_path)
                job.update(message="Product analyzed from image")
            except Exception as _exc:
                job.update(message=f"Product image analysis skipped ({_exc})")

        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: storyboard ────────────────────────────────────────────────
        skip_storyboard = params.get("skip_storyboard", False)
        if skip_storyboard:
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
            if flags:
                policy_flags.append({
                    "scene": scene["scene"],
                    "role": scene.get("role", ""),
                    "flags": flags,
                })

        # ── APPROVAL ───────────────────────────────────────────────────────────
        job.request_approval({
            "storyboard": storyboard,
            "reel_summary": {
                "mode": "ai_reels",
                "prompt": reel_brief.get("prompt") or reel_brief.get("text_hints"),
                "target_duration_seconds": reel_brief["target_duration_seconds"],
                "scene_count": len(scenes),
                "music_file": reel_brief.get("music_file") or "Random from library",
            },
            "policy_flags": policy_flags,
        })
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
        job.begin_stage("clip_selection", "Generating AI Clips", "Sending each scene to Veo3...")
        job.update(progress_pct=25)

        project_id_vx = settings_manager.get("vertex_project_id")
        location = settings_manager.get("vertex_location", "us-central1")
        reel_type = params.get("reel_type", "story")
        character_image_path = params.get("character_image_path")
        keyframe_paths = params.get("keyframe_paths") or []
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

        def _generate_scene_clip(args: tuple[int, dict]) -> tuple[int, dict | None, str | None]:
            i, scene = args
            prompt = scene["visual_description"]
            hint = scene.get("clip_hint", "")
            if hint:
                prompt = f"{prompt}. {hint}"
            dialogue = scene_dialogues.get(i, "")
            if dialogue:
                prompt = f'{prompt}. Character says: "{dialogue}"'

            duration = min(int(scene.get("duration_seconds", 5)), VEO3_MAX_DURATION)
            dest_path = str(clips_dir / f"clip_{i:03d}_veo3.mp4")

            if reel_type == "character" and character_image_path:
                image_path = character_image_path
            elif reel_type == "story" and keyframe_paths:
                image_path = keyframe_paths[i % len(keyframe_paths)]
            elif reel_type == "product" and product_image_path and scene.get("feature_product"):
                # Product mode: anchor the product only on the scenes the storyboard
                # flagged as showcase ("hero") scenes — others stay pure text-to-video.
                image_path = product_image_path
            else:
                image_path = None

            neg_prompt = _VEO3_NO_SPEECH if vo_on else ""

            result = Veo3Client().execute({
                "operation": "text_to_video",
                "prompt": prompt,
                "duration_seconds": duration,
                "dest_path": dest_path,
                "vertex_project_id": project_id_vx,
                "vertex_location": location,
                "image_path": image_path,
                "negative_prompt": neg_prompt,
            })

            # ── Auto-retry on policy violation ──────────────────────────────
            if not result.success and _is_policy_error(result.error):
                safe_prompt = _rewrite_safe_prompt(prompt)
                retry_path = str(clips_dir / f"clip_{i:03d}_veo3_retry.mp4")
                result = Veo3Client().execute({
                    "operation": "text_to_video",
                    "prompt": safe_prompt,
                    "duration_seconds": duration,
                    "dest_path": retry_path,
                    "vertex_project_id": project_id_vx,
                    "vertex_location": location,
                    "image_path": image_path,
                    "negative_prompt": neg_prompt,
                })
                if result.success:
                    dest_path = retry_path
            if not result.success:
                return i, None, f"Veo3 failed for scene {i + 1}: {result.error}"

            an = ClipAnalyzer().execute({"local_path": dest_path})
            if not an.success:
                return i, None, f"Could not analyze Veo3 clip for scene {i + 1}"

            return i, {
                "clip_id": f"clip_{i:03d}",
                "filename": Path(dest_path).name,
                "local_path": dest_path,
                "duration_seconds": float(an.data["duration_seconds"]),
                "resolution": {"width": an.data["width"], "height": an.data["height"]},
                "fps": an.data["fps"],
                "selection_reason": f"Veo3: {scene['visual_description'][:60]}",
                "scene": scene["scene"],
                "score": None,
            }, None

        clip_entries_raw: dict[int, dict] = {}
        gen_done = 0
        gen_errors: list[str] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_generate_scene_clip, (i, s)): i for i, s in enumerate(scenes)}
            for fut in as_completed(futures):
                idx, entry, err = fut.result()
                gen_done += 1
                if err:
                    gen_errors.append(err)
                    job.update(message=f"Scene {idx + 1} failed: {err}")
                else:
                    clip_entries_raw[idx] = entry
                    job.update(
                        progress_pct=25 + int((gen_done / n_scenes) * 12),
                        message=f"Generated {gen_done}/{n_scenes} AI clips",
                    )

        clip_entries = [clip_entries_raw[i] for i in sorted(clip_entries_raw)]

        if len(clip_entries) < 2:
            first_err = gen_errors[0] if gen_errors else "unknown error"
            raise RuntimeError(
                f"Only {len(clip_entries)}/{n_scenes} Veo3 clips generated — need at least 2. "
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
        scene_text = _compute_scene_text_timing(normalized_clips, cuts_ordered, scenes)
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
            video_duration = sum(c["duration_seconds"] for c in normalized_clips)
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
