"""
Pipeline runner — new flow:
  brief → storyboard → APPROVAL → voiceover → clip_selection → compose → final_review → deliver
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import MUSIC_LIBRARY_PATH, PROJECTS_DIR, SCHEMAS_DIR
from backend.job_manager import Job, JobStatus
from backend import settings_manager
from backend import edit_planner
from lib.checkpoint import append_decision, write_artifact, write_checkpoint
from lib.schema_validator import validate_artifact
from tools.ai.elevenlabs_tts import ElevenLabsTTS
from tools.analysis.audio_prober import AudioProber
from tools.analysis.clip_analyzer import ClipAnalyzer
from tools.analysis.frame_sampler import FrameSampler
from tools.analysis.video_prober import VideoProber
from tools.audio.audio_mixer import AudioMixer
from tools.koofr.koofr_browser import KoofrBrowser
from tools.koofr.koofr_downloader import KoofrDownloader
from tools.video.text_renderer import TextRenderer
from tools.video.video_composer import VideoComposer
from tools.video.video_normalizer import VideoNormalizer
from tools.video.pexels_downloader import PexelsDownloader


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_pipeline(job: Job, brief_params: dict[str, Any]) -> None:
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    local_out = settings_manager.get_local_output_dir()
    projects_root = Path(local_out).expanduser() if local_out else PROJECTS_DIR
    projects_root.mkdir(parents=True, exist_ok=True)
    project_dir = (projects_root / project_id).resolve()
    clips_dir = project_dir / "clips"
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"
    voiceover_dir = project_dir / "voiceover"

    for d in [clips_dir, tmp_dir / "normalized", output_dir, voiceover_dir]:
        d.mkdir(parents=True, exist_ok=True)

    schemas_dir = str(SCHEMAS_DIR)

    try:
        # ── STAGE 1: brief ─────────────────────────────────────────────────────
        job.begin_stage("brief", "Processing Brief", "Interpreting your request...")
        job.update(progress_pct=3, status=JobStatus.RUNNING)

        reel_brief = {
            "version": "1.0",
            "mode": brief_params.get("mode", "random"),
            "target_duration_seconds": float(brief_params.get("target_duration_seconds", 30)),
            "koofr_folder": brief_params.get("koofr_folder"),
            "clip_ids": brief_params.get("clip_ids"),
            "prompt": brief_params.get("prompt"),
            "music_file": brief_params.get("music_file"),
            "include_text": brief_params.get("include_text", True),
            "text_hints": brief_params.get("text_hints"),
        }

        write_artifact(str(projects_root), project_id, "reel_brief", reel_brief)
        write_checkpoint(str(projects_root), project_id, "brief", "completed", {"reel_brief": reel_brief})
        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: storyboard ────────────────────────────────────────────────
        job.begin_stage("storyboard", "Director's Storyboard", "Claude is planning your scene-by-scene story...")
        job.update(progress_pct=8)

        storyboard = _generate_storyboard(reel_brief)
        scenes = storyboard["scenes"]

        write_artifact(str(projects_root), project_id, "storyboard", storyboard)
        write_checkpoint(str(projects_root), project_id, "storyboard", "completed", {"storyboard": storyboard})
        job.end_stage("storyboard", f"{len(scenes)}-scene story ready — awaiting your approval")

        # ── APPROVAL: storyboard + script ──────────────────────────────────────
        job.request_approval({
            "storyboard": storyboard,
            "reel_summary": {
                "mode": reel_brief["mode"],
                "prompt": reel_brief.get("prompt") or reel_brief.get("text_hints"),
                "target_duration_seconds": reel_brief["target_duration_seconds"],
                "scene_count": len(scenes),
                "music_file": reel_brief.get("music_file") or "Random from library",
            },
        })
        approved = job.wait_for_approval(timeout=1800)
        if not approved:
            raise RuntimeError("Approval timed out after 30 minutes")

        # Merge user edits back into scenes
        resp_scenes = (job.approval_response or {}).get("scenes", [])
        edited_by_scene = {s["scene"]: s for s in resp_scenes if "scene" in s}
        for s in scenes:
            edits = edited_by_scene.get(s["scene"], {})
            if "overlay_text" in edits:
                s["overlay_text"] = edits["overlay_text"]
            if "voiceover" in edits:
                s["voiceover"] = edits["voiceover"]

        # ── STAGE 3: voiceover TTS ─────────────────────────────────────────────
        # Generate per-scene audio files now; final time-positioned track is built
        # after composition (stage 6) once we know the exact scene timestamps.
        job.begin_stage("voiceover", "Generating Voiceover", "Sending script to ElevenLabs...")
        job.update(progress_pct=20)

        scene_vo_files: dict[int, str] = {}
        scene_vo_durations: dict[int, float] = {}
        if not brief_params.get("include_voiceover", True):
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
                job.end_stage("voiceover", "No ElevenLabs key — skipping voiceover")

        # ── STAGE 4: clip_selection ────────────────────────────────────────────
        job.begin_stage("clip_selection", "Selecting Clips", "Scanning Koofr for matching clips...")
        job.update(progress_pct=25)

        browser = KoofrBrowser()
        folder = reel_brief.get("koofr_folder") or settings_manager.get_koofr_folder() or "/"
        browse_result = browser.execute({"operation": "list_clips", "path": folder, "recursive": True})
        if not browse_result.success:
            raise RuntimeError(f"Koofr browse failed: {browse_result.error}")

        mount_id = browse_result.data["mount_id"]
        available = browse_result.data["clips"]
        if not available:
            raise RuntimeError(f"No video clips found in Koofr folder '{folder}' or any sub-folders")

        mode = reel_brief["mode"]

        if mode == "browse" and reel_brief.get("clip_ids"):
            selected_koofr_clips = [c for c in available if c["file_id"] in reel_brief["clip_ids"]]
            for idx, c in enumerate(selected_koofr_clips):
                if not c.get("scene"):
                    c["scene"] = scenes[idx]["scene"] if idx < len(scenes) else idx + 1
        elif mode == "folder":
            shuffled = available.copy()
            random.shuffle(shuffled)
            selected_koofr_clips = shuffled[:len(scenes)]
            for idx, c in enumerate(selected_koofr_clips):
                c["selection_reason"] = "From selected folder"
                c["scene"] = scenes[idx]["scene"] if idx < len(scenes) else idx + 1
        elif mode == "describe":
            selected_koofr_clips = _select_clips_for_scenes(scenes, available, scene_vo_durations or None)
        else:
            # random — assign clips to scenes
            selected_koofr_clips = _select_clips_for_scenes(scenes, available, scene_vo_durations or None)

        # Parallel download + analyze
        job.update(message=f"Downloading {len(selected_koofr_clips)} clips in parallel...")

        def _dl(args):
            i, kclip = args
            dl = KoofrDownloader().execute({
                "mount_id": mount_id,
                "file_path": kclip["path"],
                "dest_dir": str(clips_dir),
                "filename": kclip["name"],
            })
            if not dl.success:
                return i, None, f"Could not download {kclip['name']}: {dl.error}"
            analysis = ClipAnalyzer().execute({"local_path": dl.data["local_path"]})
            if not analysis.success:
                return i, None, f"Could not analyze {kclip['name']}"
            return i, {
                "clip_id": f"clip_{i:03d}",
                "koofr_file_id": kclip.get("file_id", ""),
                "filename": kclip["name"],
                "local_path": dl.data["local_path"],
                "duration_seconds": analysis.data["duration_seconds"],
                "resolution": {"width": analysis.data["width"], "height": analysis.data["height"]},
                "fps": analysis.data["fps"],
                "selection_reason": kclip.get("selection_reason", "Selected"),
                "score": kclip.get("score"),
                "scene": kclip.get("scene"),
            }, None

        clip_entries_raw = {}
        n_dl = len(selected_koofr_clips)
        dl_done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_dl, (i, kc)): i for i, kc in enumerate(selected_koofr_clips)}
            for fut in as_completed(futures):
                idx, entry, err = fut.result()
                dl_done += 1
                if err:
                    job.update(message=f"Warning: {err} ({dl_done}/{n_dl})")
                else:
                    clip_entries_raw[idx] = entry
                    job.update(
                        progress_pct=25 + int((dl_done / n_dl) * 10),
                        message=f"Downloaded {dl_done}/{n_dl} clips",
                    )

        clip_entries = [clip_entries_raw[i] for i in sorted(clip_entries_raw)]

        # Pexels gap-fill
        pexels_enabled = settings_manager.get("pexels_gap_fill_enabled", False)
        if len(clip_entries) < 2 and pexels_enabled:
            job.update(message="Not enough Koofr clips — searching Pexels for gap-fill...")
            query = reel_brief.get("prompt") or "nature"
            pexels = PexelsDownloader()
            pr = pexels.execute({"query": query, "dest_dir": str(clips_dir / "pexels"),
                                  "orientation": "portrait", "min_duration": 5, "max_results": 4})
            if pr.success:
                for i, pclip in enumerate(pr.data.get("clips", [])):
                    an = ClipAnalyzer().execute({"local_path": pclip["local_path"]})
                    if an.success:
                        clip_entries.append({
                            "clip_id": f"pexels_{i:03d}",
                            "koofr_file_id": f"pexels_{pclip['pexels_id']}",
                            "filename": Path(pclip["local_path"]).name,
                            "local_path": pclip["local_path"],
                            "duration_seconds": an.data["duration_seconds"],
                            "resolution": {"width": an.data["width"], "height": an.data["height"]},
                            "fps": an.data["fps"],
                            "selection_reason": pclip["selection_reason"],
                            "score": None,
                        })

        if len(clip_entries) < 2:
            raise RuntimeError(
                f"Need at least 2 clips, got {len(clip_entries)}. "
                "Enable Pexels gap-fill in settings or add more clips to Koofr."
            )

        # Drop voiceovers for scenes whose clip failed to download.
        # Without this, those VOs would later get delay=0 and play on top of scene 1.
        if scene_vo_files:
            present_scenes = {c["scene"] for c in clip_entries if c.get("scene")}
            scene_vo_files = {k: v for k, v in scene_vo_files.items() if k in present_scenes}

        clip_manifest = {
            "version": "1.0",
            "clips": clip_entries,
            "total_available_duration_seconds": sum(c["duration_seconds"] for c in clip_entries),
        }
        write_artifact(str(projects_root), project_id, "clip_manifest", clip_manifest)
        write_checkpoint(str(projects_root), project_id, "clip_selection", "completed",
                         {"clip_manifest": clip_manifest})
        job.end_stage("clip_selection", f"{len(clip_entries)} clips ready")

        # ── STAGE 5: edit_decisions ────────────────────────────────────────────
        job.begin_stage("edit_decisions", "Planning the Edit", "ABT-aware timing + retention decisions...")
        job.update(progress_pct=40)

        target_dur = reel_brief["target_duration_seconds"]
        edit_decisions = edit_planner.plan_edit(
            clip_manifest, scenes, target_dur,
            scene_vo_durations=scene_vo_durations or None,
        )
        abt_timing = edit_decisions.get("abt_timing", {})
        abt_desc = ", ".join(f"{r}@{t}s" for r, t in abt_timing.items()) if abt_timing else ""
        write_artifact(str(projects_root), project_id, "edit_decisions", edit_decisions)
        write_checkpoint(str(projects_root), project_id, "edit_decisions", "completed",
                         {"edit_decisions": edit_decisions})
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

        def _normalize_cut(cut):
            clip = clip_by_id[cut["clip_id"]]
            norm_path = str(tmp_dir / "normalized" / f"{cut['order']:02d}_{cut['clip_id']}.mp4")
            result = VideoNormalizer().execute({
                "input_path": clip["local_path"],
                "output_path": norm_path,
                "trim_in": cut["trim_in"],
                "trim_out": cut["trim_out"],
            })
            return cut, clip, norm_path, result

        normalized_by_order = {}
        with ThreadPoolExecutor(max_workers=min(4, n_cuts)) as pool:
            futures = {pool.submit(_normalize_cut, cut): cut for cut in cuts_ordered}
            for fut in as_completed(futures):
                cut, clip, norm_path, n = fut.result()
                norm_done += 1
                pct = 45 + int((norm_done / n_cuts) * 20)
                if not n.success:
                    raise RuntimeError(f"Normalize failed for {clip['filename']}: {n.error}")
                job.update(progress_pct=pct, message=f"Normalized {norm_done}/{n_cuts} clips")
                normalized_by_order[cut["order"]] = {
                    "path": norm_path,
                    "duration_seconds": cut["trim_out"] - cut["trim_in"],
                    "transition": cut["transition"],
                    "transition_duration": cut.get("transition_duration_seconds", 0.5),
                }

        normalized_clips = [normalized_by_order[i] for i in sorted(normalized_by_order)]

        job.update(progress_pct=66, message=f"Joining {len(normalized_clips)} clips with transitions...")
        composer = VideoComposer()
        composed_path = str(tmp_dir / "composed.mp4")
        compose_result = composer.execute({"clips": normalized_clips, "output_path": composed_path})
        if not compose_result.success:
            raise RuntimeError(f"Composition failed: {compose_result.error}")

        # Per-scene text overlays — timed to each clip's position in the timeline
        job.update(progress_pct=78, message="Rendering per-scene text overlays...")
        scene_text = _compute_scene_text_timing(normalized_clips, cuts_ordered, scenes)
        titled_path = composed_path
        if scene_text:
            titled_path = str(tmp_dir / "titled.mp4")
            tr = TextRenderer()
            tr_result = tr.execute({
                "input_path": composed_path,
                "output_path": titled_path,
                "scenes": scene_text,
            })
            if not tr_result.success:
                titled_path = composed_path
                job.update(message=f"Warning: text overlay failed, skipping: {tr_result.error}")

        # Position voiceover now that we know exact scene timestamps
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
        final_path = str(output_dir / "reel_final.mp4")
        music_path = _resolve_music(reel_brief.get("music_file"))

        mixer = AudioMixer()
        mix_result = mixer.execute({
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

        prober = VideoProber()
        probe = prober.execute({"path": final_path})
        probe_data = probe.data if probe.success else {}

        frame_sampler = FrameSampler()
        frame_sampler.execute({
            "video_path": final_path,
            "output_dir": str(project_dir / "review" / "frames"),
            "num_frames": 4,
        })

        audio_prober = AudioProber()
        audio_check = audio_prober.execute({"path": final_path})
        audio_data = audio_check.data if audio_check.success else {}

        actual_dur = probe_data.get("duration_seconds", 0)
        within_tol = abs(actual_dur - target_dur) <= (target_dur * 0.10)

        findings = []
        if probe_data.get("width") != 1080 or probe_data.get("height") != 1920:
            findings.append({
                "severity": "critical",
                "check": "technical_probe",
                "description": f"Resolution is {probe_data.get('width')}x{probe_data.get('height')}, expected 1080x1920",
                "recommended_action": "Re-normalize clips",
            })

        review_status = "fail" if any(f["severity"] == "critical" for f in findings) else "pass"

        final_review = {
            "version": "1.0",
            "status": review_status,
            "checks": {
                "technical_probe": {
                    "resolution": probe_data.get("resolution", ""),
                    "codec": probe_data.get("codec", ""),
                    "fps": probe_data.get("fps", 0),
                    "duration_seconds": actual_dur,
                    "file_size_bytes": probe_data.get("file_size_bytes", 0),
                    "is_playable": probe_data.get("is_playable", False),
                },
                "duration_check": {"target_seconds": target_dur, "actual_seconds": actual_dur,
                                   "within_tolerance": within_tol},
                "audio_check": {"voiceover_added": voiceover_path is not None,
                                "music_added": music_path is not None},
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
        write_checkpoint(str(projects_root), project_id, "final_review", "completed",
                         {"final_review": final_review})
        job.end_stage("final_review", "All quality checks passed")

        # ── STAGE 8: deliver ───────────────────────────────────────────────────
        job.begin_stage("deliver", "Ready!", "Your reel is ready for download")
        job.update(progress_pct=99)

        file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0

        write_artifact(str(projects_root), project_id, "publish_log", {
            "version": "1.0",
            "local_path": final_path,
            "file_size_bytes": file_size,
            "duration_seconds": actual_dur,
            "timestamp": _ts(),
        })

        job.end_stage("deliver", "Done")
        job.update(
            status=JobStatus.COMPLETED,
            progress_pct=100,
            message=f"Reel ready! Saved to {str(project_dir)}",
            result={
                "project_id": project_id,
                "output_path": final_path,
                "project_dir": str(project_dir),
                "duration_seconds": actual_dur,
                "file_size_bytes": file_size,
                "clips_used": len(clip_entries),
                "voiceover": voiceover_path is not None,
                "scenes": [
                    {"scene": s["scene"], "overlay_text": s.get("overlay_text", ""),
                     "voiceover": s.get("voiceover", "")}
                    for s in scenes
                ],
                "final_review": final_review,
            },
        )

    except Exception as exc:
        job.update(status=JobStatus.FAILED, message="Pipeline failed", error=str(exc))
        raise


# ── Storyboard generation ──────────────────────────────────────────────────────

# ── Script frameworks ──────────────────────────────────────────────────────────
# Each framework defines its narrative structure, the per-scene role vocabulary,
# the JSON role enum, and the "map scenes to roles" instruction. ABT is the default
# and what every non-AI pipeline uses; the AI Reels UI lets the user pick another.
_FRAMEWORKS: dict[str, dict] = {
    "abt": {
        "name": "ABT",
        "structure": """FRAMEWORK — ABT (And, But, Therefore):
Every reel must follow this three-part structure:
- AND (setup): Establish the world the viewer already lives in. Build identity match — "this is for me."
- BUT (tension): The open loop. The contradiction. The thing that's broken, misunderstood, or overlooked. This is where retention is won or lost. The viewer's brain cannot close the tab until this is resolved.
- THEREFORE (resolution): The payoff. The product/brand/idea enters HERE — as the earned solution, never before. If the "But" was a real problem, the "Therefore" should feel like a genuine unlock.""",
        "roles_block": """SCENE ROLES to use:
- "hook" — 0–3s, visual+audio pattern interrupt, tension promise (no brand, no product)
- "and" — 3–8s, setup/world, identity match ("most people…", "if you've been doing X…")
- "but" — 8–18s, the open loop, the contradiction, the real problem (must feel genuinely surprising)
- "therefore" — 18–27s, the payoff/resolution, product enters here as the earned solution
- "trigger" — 27–30s, CTA or micro-revelation that triggers shares ("most people don't know this part…")""",
        "role_enum": "hook|and|but|therefore|trigger",
        "map_line": "Map scenes to ABT roles (hook → and → but → therefore → trigger). "
                    "For shorter reels compress: hook+and in scene 1, but in scene 2, therefore+trigger in final scene.",
    },
    "pas": {
        "name": "PAS",
        "structure": """FRAMEWORK — PAS (Problem, Agitate, Solve):
- PROBLEM: Open on a pain point the viewer instantly recognizes as their own.
- AGITATE: Twist the knife — make the cost of the problem vivid and emotional, stack the frustration.
- SOLVE: Introduce the product/idea as the relief. The payoff must feel proportional to the agitation.""",
        "roles_block": """SCENE ROLES to use:
- "problem" — 0–5s, name the viewer's pain, identity match, no brand
- "agitate" — 5–15s, amplify the problem, show what it costs, build emotional pressure
- "solve" — 15–25s, the product/idea enters as the fix
- "cta" — 25–30s, directive to act or share""",
        "role_enum": "problem|agitate|solve|cta",
        "map_line": "Map scenes to PAS roles (problem → agitate → solve → cta).",
    },
    "aida": {
        "name": "AIDA",
        "structure": """FRAMEWORK — AIDA (Attention, Interest, Desire, Action):
- ATTENTION: A pattern-interrupt hook that stops the scroll.
- INTEREST: Feed curiosity with an intriguing detail or a fresh angle.
- DESIRE: Make them want it — benefits, transformation, social proof.
- ACTION: A clear, specific call to action.""",
        "roles_block": """SCENE ROLES to use:
- "attention" — 0–3s, scroll-stopping pattern interrupt, no brand
- "interest" — 3–10s, build curiosity with a fresh angle or detail
- "desire" — 10–22s, stoke want through benefits/transformation/proof
- "action" — 22–30s, clear specific CTA""",
        "role_enum": "attention|interest|desire|action",
        "map_line": "Map scenes to AIDA roles (attention → interest → desire → action).",
    },
    "hvc": {
        "name": "Hook-Value-CTA",
        "structure": """FRAMEWORK — Hook / Value / CTA (high-retention short-form):
- HOOK: 0–3s pattern interrupt that promises a payoff.
- VALUE: Deliver the useful or entertaining payoff fast and concretely — the reason to keep watching.
- CTA: One clear directive (follow, save, share, shop).""",
        "roles_block": """SCENE ROLES to use:
- "hook" — 0–3s, pattern interrupt + promise, no brand
- "value" — 3–24s, deliver the payoff concretely (may span multiple scenes)
- "cta" — 24–30s, one clear directive""",
        "role_enum": "hook|value|cta",
        "map_line": "Map scenes to Hook-Value-CTA roles (hook → value → … → cta). "
                    "The value section can span several scenes.",
    },
    "hpsp": {
        "name": "Hook→Problem→Shift→Payoff",
        "structure": """FRAMEWORK — Hook → Problem → Shift → Payoff:
- HOOK (0–3s): A bold, shocking truth or pattern interrupt that shatters expectations. Zero filler words. e.g. "Everything you know about X is a lie."
- PROBLEM (3–8s): Make the viewer feel understood — name the struggle or mistake they make every day. e.g. "You're doing X daily and it's quietly destroying your progress."
- SHIFT (8–15s): Shatter the old belief with an actionable counter-perspective or breakthrough. e.g. "But what if doing less is the real secret?"
- PAYOFF + CTA (15–30s): Give the clear actionable solution, then a directive. e.g. "Try X. Share this with someone who needs it.\"""",
        "roles_block": """SCENE ROLES to use:
- "hook" — 0–3s, bold shocking truth / pattern interrupt, no filler, no brand
- "problem" — 3–8s, name the viewer's daily mistake, make them feel understood
- "shift" — 8–15s, shatter the old belief with a counter-perspective / breakthrough
- "payoff" — 15–30s, actionable solution + CTA directive (share/save/try)""",
        "role_enum": "hook|problem|shift|payoff",
        "map_line": "Map scenes to Hook→Problem→Shift→Payoff roles (hook → problem → shift → payoff).",
    },
}

_HOOK_ARCHITECTURE = """HOOK ARCHITECTURE (first 3 seconds — fire all three layers simultaneously):
1. Visual: A relatable before-state, a surprising result, or a pattern interrupt — NOT a product shot.
2. Audio/Text overlay: Lead with the insight or tension, NOT the brand name. Never open with "Hey guys" or the brand name.
3. Edit: A sharp cut, zoom into detail, or audio drop that stops passive scrollers."""

_RETENTION_RULES = """RETENTION RULES:
- Get to the core tension or value within 7 seconds — the setup must never drag
- Add a re-hook or pattern interrupt every 8–10 seconds (new visual cut, text overlay, tonal shift)
- Bury a re-hook just before the 7–10 second mark (the "Valley of Disappointment" drop zone)
- Use the Zeigarnik Effect: one open loop per reel — always close it before the end
- One idea per reel — never pack multiple benefits"""

_VOICEOVER_RULES = """VOICEOVER WRITING RULES (writing for ears, not eyes):
- Short sentences. Then shorter. Vary rhythm deliberately.
- No passive voice. Active, present, direct — always.
- Say "you" constantly — never "people", "customers", "users"
- Front-load the tension — lead with the interesting thing, fill in context after
- Product/brand enters only at the resolution/payoff — never in the hook or setup
- Read every line aloud — if it doesn't flow naturally spoken, it won't flow naturally heard"""


def _generate_storyboard(reel_brief: dict) -> dict:
    import anthropic

    target_dur = reel_brief["target_duration_seconds"]
    n_scenes = max(3, min(8, int(target_dur / 5)))
    prompt = reel_brief.get("prompt") or reel_brief.get("text_hints") or "an engaging visual story"
    use_brand = reel_brief.get("use_brand_guidelines", True)
    brand_voice = settings_manager.get_brand_voice() if use_brand else ""
    context = f"\n\nBrand voice:\n{brand_voice[:300]}" if brand_voice else ""

    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")
    if not api_key:
        return _default_storyboard(reel_brief, n_scenes)

    dur_per = round(target_dur / n_scenes, 1)

    _google_policy_section = """
CRITICAL — GOOGLE VEO3 CONTENT POLICY (AI REELS MODE):
visual_description and clip_hint are sent directly to Google Veo3. Two strict rules:

RULE 1 — ENGLISH ONLY for visual_description and clip_hint.
These fields must be written in English only — no Hindi, no Hinglish, no Devanagari.
overlay_text and voiceover can be Hinglish as usual. Only the visual fields must be English.

RULE 2 — NEVER USE AGE OR MINOR-SUGGESTING WORDS (CRITICAL — error code 58061214).
Google Veo3 flags minor-related content categorically. Never write: girl, girls, young girl,
teenage, teen, minor, child, kid, adolescent, juvenile, ladki, bachi, college girl, school girl.
Always use: woman, person, individual, figure, someone, adult. Even "young woman" is safer than "girl".
This is the #1 cause of policy violations for lifestyle/fashion content.

RULE 3 — AVOID ALL CONTENT POLICY TRIGGER WORDS in visual_description and clip_hint.
Google's safety filters BLOCK prompts containing:
- Violence: kill, murder, blood, gore, dead, death, dying, weapon, gun, bomb, stab, slash, war, combat, torture, execute, brutality, attack, assault, fight, beat up
- Self-harm: suicide, self-harm, cutting, overdose
- Sexual: nude, naked, explicit, pornographic, erotic, nsfw, sexual
- Hate: slurs, supremacy, bigotry, fascist, dehumanize, subhuman
- Hinglish equivalents that translate to the above: maar, maarna, marna, khoon, ladai, jhagda, tabahi, barbaad, naanga, hatya, danga

Safe alternatives: describe emotions through body language and environment instead.
Frustration → "person with clenched jaw, pacing in a cluttered room"
Urgency → "rapid close-up shots of hands moving fast, clock ticking"
Anger → "wide eyes, sharp breath, turning away from camera"
Excitement → "person jumping, arms raised, bright open space"
"""

    _is_ai_reels = reel_brief.get("mode") == "ai_reels"
    _no_characters = _is_ai_reels and reel_brief.get("reel_type") == "none"
    _is_product = _is_ai_reels and reel_brief.get("reel_type") == "product"
    _is_ugc = _is_ai_reels and reel_brief.get("reel_type") == "ugc"
    _product_desc = reel_brief.get("product_description", "")

    _product_section = (f"""
PRODUCT MODE — BUILD THE STORY AROUND THIS PRODUCT:
The reel must sell/showcase this specific product:
  {_product_desc or '(see the topic/prompt)'}
- Weave the product naturally into the narrative arc of the chosen framework — it is the payoff/solution.
- Mark the scenes that visually showcase the product with "feature_product": true (typically the
  resolution/payoff and 1–2 hero shots). Mark setup/tension scenes that do NOT show the product
  "feature_product": false.
- For feature_product scenes, the visual_description should center the product (its look, use, detail).
- Keep visual_description in English (Veo3 rule above).
""") if _is_product else ""

    _ugc_section = (f"""
UGC MODE — AUTHENTIC CREATOR DEMO (a real person reviewing/showing the product):
This is a user-generated-content style reel. ONE on-camera creator presents and demonstrates
this product, talking to the camera like a genuine personal recommendation:
  PRODUCT: {_product_desc or '(see the topic/prompt)'}
- Write the voiceover as a FIRST-PERSON, casual, authentic creator monologue — never an ad
  voiceover. Use natural spoken language ("okay so", "honestly", "wait — look at this").
- The SAME creator appears in EVERY scene, holding / using / showing the product to camera.
- Every visual_description MUST show the creator AND the product together — describe the person's
  action with the product (e.g. "young woman holding the tan tote bag up to camera, smiling, casual
  sunlit room"), the framing, and the vibe. Keep it natural and selfie/handheld in feel.
- Mark product close-up / showcase beats "feature_product": true; talking-head beats "feature_product": false.
- Keep visual_description in English (Veo3 rule above).
""") if _is_ugc else ""

    _no_characters_section = """
CRITICAL — NO CHARACTERS MODE (ZERO HUMANS ON SCREEN):
This reel must contain NO people, humans, characters, faces, hands, or body parts in ANY scene.
Every visual_description and clip_hint MUST describe ONLY: objects, products, environments, landscapes,
textures, close-ups of materials, nature, light, still life, abstract motion, or the subject itself.
- NEVER write: person, people, woman, man, girl, boy, hand, hands, finger, figure, someone, model,
  character, crowd, face, portrait, he, she, they.
- Replace every human action with object or environment motion. Instead of "a woman painting sunflowers",
  write "a brush gliding across canvas, yellow paint blooming into sunflower petals under warm light".
- Convey emotion through lighting, color, weather, camera movement, and the subject — never through a person.
This overrides any human-centric guidance above. The chosen retention structure still applies, but it is told
ENTIRELY through objects and scenery, with zero human presence in the generated visuals.
"""

    fw = _FRAMEWORKS.get(reel_brief.get("framework", "abt"), _FRAMEWORKS["abt"])

    system = (
        "You are an expert Instagram Reels scriptwriter. You write scripts for RETENTION, not just views.\n\n"
        + fw["structure"] + "\n\n"
        + _HOOK_ARCHITECTURE + "\n\n"
        + _RETENTION_RULES + "\n\n"
        + _VOICEOVER_RULES + "\n\n"
        + fw["roles_block"] + "\n"
        + (_google_policy_section if _is_ai_reels else "")
        + (_no_characters_section if _no_characters else "")
        + (_product_section if _is_product else "")
        + (_ugc_section if _is_ugc else "")
        + "\nRespond ONLY with valid JSON — no markdown fences."
    )

    user = (
        f"Write a {n_scenes}-scene {fw['name']}-structured storyboard for this Instagram Reel.\n"
        f"Topic/prompt: {prompt}{context}\n"
        + ("REMINDER: NO CHARACTERS MODE is active — every visual_description and clip_hint must show "
           "ZERO humans/people. Describe only objects, scenery, textures, and the subject.\n" if _no_characters else "")
        + f"Total duration: {target_dur}s (~{dur_per}s per scene)\n\n"
        + fw["map_line"] + "\n\n"
        'Return JSON: {"theme": "one-line theme", "scenes": [{'
        '"scene": 1, '
        f'"role": "{fw["role_enum"]}", '
        '"visual_description": "specific visual direction — what the clip must show, not generic", '
        '"clip_hint": "2-4 keywords to find a matching clip (be specific)", '
        f'"duration_seconds": {dur_per}, '
        '"overlay_text": "Punchy on-screen text max 8 words — front-load tension", '
        '"voiceover": "Spoken line — short sentences, say you, active voice, no brand name in hook/setup"'
        + (', "feature_product": true' if (_is_product or _is_ugc) else "")
        + "}]}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": user}],
        system=system,
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _default_storyboard(reel_brief, n_scenes)
    scenes = data.get("scenes", [])
    if not scenes:
        return _default_storyboard(reel_brief, n_scenes)
    # Normalize durations to sum to target
    total = sum(s.get("duration_seconds", target_dur / n_scenes) for s in scenes)
    scale = target_dur / max(total, 1)
    for s in scenes:
        s["duration_seconds"] = round(s.get("duration_seconds", target_dur / n_scenes) * scale, 1)
    data["scenes"] = scenes
    return data


def _default_storyboard(reel_brief: dict, n_scenes: int) -> dict:
    target_dur = reel_brief["target_duration_seconds"]
    dur_per = round(target_dur / n_scenes, 1)
    roles = ["opening", "build-up", "peak", "build-up", "peak", "outro"]
    return {
        "theme": reel_brief.get("prompt") or "Visual story",
        "scenes": [
            {
                "scene": i + 1,
                "role": roles[i % len(roles)],
                "visual_description": "Compelling visual moment",
                "clip_hint": "",
                "duration_seconds": dur_per,
                "overlay_text": "",
                "voiceover": "",
            }
            for i in range(n_scenes)
        ],
    }


# ── Cinematic keyframe director ────────────────────────────────────────────────

def _cinematic_role_for(idx: int, total: int) -> str:
    """Map a shot index onto the 4-beat arc (setup → build → turn → payoff)."""
    if idx == 0:
        return "hook"
    if idx == total - 1:
        return "outro"
    pos = idx / max(total - 1, 1)
    if pos < 0.4:
        return "and"        # setup / build
    if pos < 0.7:
        return "but"        # turn
    return "therefore"      # payoff


def _shot_to_scene(shot: dict, idx: int, total: int) -> dict:
    """Project one §5 shot onto the existing storyboard scene shape so the approval
    modal, VO stage, and edit planner run unchanged. Cinematography fields are carried
    along so the modal can surface them."""
    return {
        "scene": idx + 1,
        "kf_id": shot.get("kf_id", f"KF{idx + 1}"),
        "role": _cinematic_role_for(idx, total),
        "visual_description": shot.get("image_prompt", ""),
        "clip_hint": " ".join(filter(None, [shot.get("shot_type", ""), shot.get("camera_move", "")])),
        "duration_seconds": float(shot.get("duration_sec", 2.0)),
        "overlay_text": shot.get("overlay_text", ""),
        "voiceover": shot.get("vo_text", ""),
        # cinematography meta (surfaced in the approval modal, not used by Veo yet)
        "shot_type": shot.get("shot_type", ""),
        "lens_mm": shot.get("lens_mm"),
        "dof": shot.get("dof", ""),
        "camera_move": shot.get("camera_move", ""),
        "transition_out": shot.get("transition_out", "cut"),
        "needs_end_frame": bool(shot.get("needs_end_frame", False)),
        "speaking_on_camera": bool(shot.get("speaking_on_camera", False)),
    }


def _default_cinematic_storyboard(reel_brief: dict, n_shots: int) -> dict:
    """Deterministic fallback contract when Claude is unavailable or returns junk."""
    target_dur = reel_brief["target_duration_seconds"]
    dur_per = round(target_dur / n_shots, 1)
    style_lock = "cinematic film still, Kodak Portra 400 grain, soft directional natural light, warm grade"
    shot_types = ["ELS", "MS", "CU", "ECU", "Low", "MLS", "MCU", "High", "LS", "MS", "CU", "ECU"]
    moves = ["locked", "push", "track", "locked", "pull", "pan", "locked", "orbit", "gimbal", "locked", "push", "locked"]
    shots = []
    for i in range(n_shots):
        shots.append({
            "kf_id": f"KF{i + 1}",
            "duration_sec": dur_per,
            "shot_type": shot_types[i % len(shot_types)],
            "lens_mm": 35,
            "dof": "shallow",
            "camera_move": moves[i % len(moves)],
            "image_prompt": "Cinematic frame of the subject in a richly lit environment",
            "video_prompt": "Subtle, deliberate camera motion; the subject holds presence",
            "vo_text": "",
            "overlay_text": "",
            "speaking_on_camera": False,
            "transition_out": "cut",
            "needs_end_frame": False,
            "end_image_prompt": None,
        })
    return {
        "version": "1.0",
        "theme": reel_brief.get("prompt") or "Cinematic sequence",
        "style_lock": style_lock,
        "reference_image": reel_brief.get("character_image_path"),
        "aspect_ratio": "9:16",
        "shots": shots,
        "scenes": [_shot_to_scene(s, i, n_shots) for i, s in enumerate(shots)],
    }


def _generate_cinematic_storyboard(reel_brief: dict) -> dict:
    """
    Cinematographer director for the Cinematic reel type. Takes a single reference
    image + storyline + duration and emits the §5 per-shot contract: one global
    style_lock plus 9–12 shots with shot_type / lens / DOF / camera_move / cut.

    The system GENERATES its own keyframe stills downstream (M2) — this stage only
    produces the contract. Output is validated against cinematic_storyboard.schema.json
    and projected onto the existing storyboard scene shape for the rest of the pipeline.
    """
    import anthropic

    target_dur = reel_brief["target_duration_seconds"]
    # Shot count derives from the configured target shot length. Video bills per second
    # at a ~5s floor per clip, so fewer/longer shots = fewer clips billed = less waste.
    # Clamped 4–12 so the 4-beat arc + required shot variety still fit.
    shot_seconds = float(settings_manager.get("cinematic_shot_seconds", 5.0) or 5.0)
    n_shots = max(4, min(12, int(round(target_dur / max(shot_seconds, 1.5)))))
    prompt = reel_brief.get("prompt") or reel_brief.get("text_hints") or "a cinematic character sequence"
    use_brand = reel_brief.get("use_brand_guidelines", True)
    brand_voice = settings_manager.get_brand_voice() if use_brand else ""
    context = f"\n\nBrand voice:\n{brand_voice[:300]}" if brand_voice else ""
    dur_per = round(target_dur / n_shots, 1)

    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")
    if not api_key:
        return _default_cinematic_storyboard(reel_brief, n_shots)

    # Same Veo3/Nano content policy that governs the AI-Reels storyboard: English-only
    # visual prompts, never minor/age terms, avoid violence/self-harm/sexual triggers.
    policy = """
CONTENT POLICY (image_prompt and video_prompt are sent to image/video models):
- ENGLISH ONLY for image_prompt and video_prompt. vo_text may be Hinglish.
- NEVER use age/minor words (girl, teen, child, kid, ladki, bachi, ...). Use woman, person, adult, figure.
- Avoid violence/self-harm/sexual/hate trigger words; convey emotion through body language, light, and environment.
"""

    system = (
        "You are a CINEMATOGRAPHER-DIRECTOR shooting a short cinematic sequence for a 9:16 reel.\n"
        "You think in shots, lenses, depth of field, camera movement, and cuts — like a film DP.\n\n"
        "Produce a STRICT JSON contract with ONE global style_lock and an array of shots.\n\n"
        "HARD RULES:\n"
        f"- Exactly {n_shots} shots, assembling to a clean 4-beat arc: setup → build → turn → payoff.\n"
        "- Must include AT LEAST: one establishing wide (ELS/LS), one intimate close-up (CU/MCU),\n"
        "  one ECU detail (Insert/ECU), and one power angle (Low or High).\n"
        "- style_lock is authored ONCE (film stock/grain, location, wardrobe, palette, light) and is\n"
        "  appended to every prompt — it MUST NOT vary per shot. Do NOT repeat it inside image_prompt.\n"
        "- The SAME character/wardrobe from the reference image appears in every shot.\n"
        "- Default speaking_on_camera=false; frame narration shots as listening / breathing / observing,\n"
        "  NOT talking. Only set true with clear justification (we have no lip-sync).\n"
        "- transition_out is one of cut/dissolve/match_cut (prefer cut).\n"
        "- Set needs_end_frame=true only for shots with strong motion that benefit from start+end\n"
        "  keyframe interpolation; then end_image_prompt is REQUIRED, else null.\n"
        "- overlay_text: short punchy ON-SCREEN caption for this beat (≤6 words, front-load tension).\n"
        "  Carry the narrative like a captioned reel. Use \"\" for pure-visual beats — NOT every shot\n"
        "  needs text (aim for ~half). overlay_text may be Hinglish (brand voice); keep it tight.\n"
        + policy +
        "\nRespond ONLY with valid JSON — no markdown fences.\n"
        "Shape: {\"style_lock\": \"...\", \"aspect_ratio\": \"9:16\", \"shots\": [{"
        "\"kf_id\": \"KF1\", \"duration_sec\": <num>, \"shot_type\": \"ELS|LS|MLS|MS|MCU|CU|ECU|Low|High|Insert\", "
        "\"lens_mm\": <num>, \"dof\": \"shallow|medium|deep\", "
        "\"camera_move\": \"locked|push|pull|pan|track|orbit|gimbal\", "
        "\"image_prompt\": \"still framing for this beat (English)\", "
        "\"video_prompt\": \"motion/camera direction for the video model (English)\", "
        "\"vo_text\": \"spoken line for this beat (may be empty)\", "
        "\"overlay_text\": \"short on-screen caption ≤6 words, or empty\", "
        "\"speaking_on_camera\": false, \"transition_out\": \"cut\", "
        "\"needs_end_frame\": false, \"end_image_prompt\": null}]}"
    )

    user = (
        f"Storyline: {prompt}{context}\n"
        f"Target duration: {target_dur}s across {n_shots} shots (~{dur_per}s each).\n"
        "Write the cinematic shot contract now."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
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
    except (anthropic.APIError, json.JSONDecodeError, KeyError, IndexError):
        return _default_cinematic_storyboard(reel_brief, n_shots)

    shots = data.get("shots", [])
    if not shots:
        return _default_cinematic_storyboard(reel_brief, n_shots)

    # Normalize shot durations to sum to target (Veo caps each at 8s).
    total = sum(float(s.get("duration_sec", dur_per)) for s in shots)
    scale = target_dur / max(total, 1)
    for i, s in enumerate(shots):
        s["duration_sec"] = round(min(float(s.get("duration_sec", dur_per)) * scale, 8.0), 1)
        s.setdefault("kf_id", f"KF{i + 1}")
        s.setdefault("speaking_on_camera", False)
        s.setdefault("transition_out", "cut")
        s.setdefault("needs_end_frame", False)
        s.setdefault("end_image_prompt", None)
        s.setdefault("vo_text", "")
        s.setdefault("overlay_text", "")

    contract = {
        "version": "1.0",
        "theme": data.get("theme") or (prompt[:60].strip()),
        "style_lock": data.get("style_lock", ""),
        "reference_image": reel_brief.get("character_image_path"),
        "aspect_ratio": data.get("aspect_ratio", "9:16"),
        "shots": shots,
        "scenes": [_shot_to_scene(s, i, len(shots)) for i, s in enumerate(shots)],
    }

    # Validate against the schema; fall back to default if the director drifted off-contract.
    errors = validate_artifact(str(SCHEMAS_DIR), "cinematic_storyboard", contract)
    if errors:
        fallback = _default_cinematic_storyboard(reel_brief, n_shots)
        fallback["_validation_errors"] = errors[:8]
        return fallback

    return contract


# ── Clip selection per scene ───────────────────────────────────────────────────

def _fuzzy_clip_score(hint: str, clip: dict) -> float:
    """
    Score how well a clip_hint matches a clip.
    Uses token-level exact + partial match + difflib sequence similarity.
    Searches both filename and folder path so "beach/waves.mp4" gets
    credit for 'beach' even if the filename alone doesn't contain it.
    """
    from difflib import SequenceMatcher

    if not hint:
        return 0.0

    hint_tokens = [w for w in hint.lower().split() if len(w) > 2]
    if not hint_tokens:
        return 0.0

    # Build a searchable text from full path (captures folder names)
    clip_path = (clip.get("path") or clip.get("name") or "").lower()
    clip_text = clip_path.replace("_", " ").replace("-", " ").replace("/", " ").replace(".", " ")
    clip_tokens = clip_text.split()

    score = 0.0
    for h in hint_tokens:
        best_token_score = 0.0
        for c in clip_tokens:
            # Exact match: highest reward
            if h == c:
                best_token_score = max(best_token_score, 2.0)
            # Substring match (both directions)
            elif h in c or c in h:
                best_token_score = max(best_token_score, 1.2)
            else:
                # Sequence similarity — rewards close spellings ("waves" ~ "wavy")
                ratio = SequenceMatcher(None, h, c).ratio()
                if ratio >= 0.7:
                    best_token_score = max(best_token_score, ratio)
        score += best_token_score

    # Normalize so scores are comparable across different hint lengths
    return score / len(hint_tokens)


def _select_clips_for_scenes(
    scenes: list[dict],
    available: list[dict],
    scene_vo_durations: dict[int, float] | None = None,
) -> list[dict]:
    """
    Match each scene to the best available clip via fuzzy clip_hint search, no repeats.
    When scene_vo_durations is provided, clips shorter than 80% of the VO duration are
    skipped so the selected clip can actually contain the full voiceover. If no clip
    meets the length requirement, the longest unused clip is used as a fallback.
    """
    pool = available.copy()
    random.shuffle(pool)  # randomize tie-breaking
    used: set[str] = set()
    selected: list[dict] = []
    vo_map = scene_vo_durations or {}

    for scene in scenes:
        hint = (scene.get("clip_hint") or "").strip()
        vo_dur = vo_map.get(scene["scene"], 0.0)
        min_clip_dur = vo_dur * 0.8 if vo_dur > 0 else 0.0

        best = None
        best_score = -1.0

        for clip in pool:
            uid = clip.get("file_id") or clip["name"]
            if uid in used:
                continue
            # Skip clips too short to cover the voiceover (only when duration is known)
            clip_dur = clip.get("duration_seconds", 0)
            if min_clip_dur > 0 and clip_dur > 0 and clip_dur < min_clip_dur:
                continue
            s = _fuzzy_clip_score(hint, clip)
            if s > best_score:
                best_score = s
                best = clip

        # Fallback 1: relax duration constraint — take best-scoring unused clip of any length
        if best is None:
            for clip in pool:
                uid = clip.get("file_id") or clip["name"]
                if uid in used:
                    continue
                s = _fuzzy_clip_score(hint, clip)
                if s > best_score:
                    best_score = s
                    best = clip

        # Fallback 2: no hint / all tied — take longest unused clip (or first if durations unknown)
        if best is None:
            best = max(
                (c for c in pool if (c.get("file_id") or c["name"]) not in used),
                key=lambda c: c.get("duration_seconds", 0),
                default=None,
            )

        if best:
            uid = best.get("file_id") or best["name"]
            reason = (
                f"[{scene['role']}] hint='{hint}' score={best_score:.2f}"
                if hint else f"[{scene['role']}] random fallback"
            )
            best["selection_reason"] = reason
            best["scene"] = scene["scene"]
            used.add(uid)
            selected.append(best)

    return selected


# ── ElevenLabs voiceover ───────────────────────────────────────────────────────

def _probe_audio_duration(path: str) -> float:
    """Return duration of an audio file in seconds via ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _generate_scene_vo_files(
    scenes: list[dict],
    voiceover_dir: Path,
) -> tuple[dict[int, str], dict[int, float]]:
    """Generate one MP3 per scene. Returns ({scene_num: path}, {scene_num: duration_s})."""
    tts = ElevenLabsTTS()
    files: dict[int, str] = {}
    durations: dict[int, float] = {}
    for scene in scenes:
        text = (scene.get("voiceover") or "").strip()
        if not text:
            continue
        out = str(voiceover_dir / f"vo_scene_{scene['scene']:02d}.mp3")
        r = tts.execute({"text": text, "output_path": out})
        if r.success:
            files[scene["scene"]] = out
            durations[scene["scene"]] = _probe_audio_duration(out)
    return files, durations


def _generate_voiceovers(scenes: list[dict], voiceover_dir: Path) -> str | None:
    tts = ElevenLabsTTS()
    audio_files = []

    for scene in scenes:
        text = (scene.get("voiceover") or "").strip()
        if not text:
            continue
        out = str(voiceover_dir / f"vo_scene_{scene['scene']:02d}.mp3")
        result = tts.execute({"text": text, "output_path": out})
        if result.success:
            audio_files.append(out)

    if not audio_files:
        return None
    if len(audio_files) == 1:
        return audio_files[0]

    # Concatenate all scene voiceovers
    concat_path = str(voiceover_dir / "voiceover_full.mp3")
    list_path = str(voiceover_dir / "vo_list.txt")
    with open(list_path, "w") as f:
        for af in audio_files:
            f.write(f"file '{af}'\n")

    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c:a", "libmp3lame", "-q:a", "2", concat_path],
        capture_output=True, text=True, timeout=120,
    )
    return concat_path if r.returncode == 0 else audio_files[0]


# ── Edit decisions from scenes ─────────────────────────────────────────────────

def _plan_edit_from_scenes(clip_manifest: dict, scenes: list[dict], target_duration: float) -> dict:
    clips = clip_manifest["clips"]
    n = len(clips)
    transition_overhead = (n - 1) * 0.5
    usable = target_duration - transition_overhead

    # Use scene durations as weights for distributing clip time
    scene_durs = [s.get("duration_seconds", usable / n) for s in scenes]
    total_scene = sum(scene_durs)
    scale = usable / max(total_scene, 1)

    transition_cycle = ["cross_dissolve", "hard_cut", "fade_to_black", "hard_cut"]
    cuts = []
    for i, clip in enumerate(clips):
        desired = round(scene_durs[i] * scale, 2) if i < len(scene_durs) else round(usable / n, 2)
        trim_in = 0.0
        if clip["duration_seconds"] > desired * 3:
            trim_in = round(clip["duration_seconds"] * 0.2, 2)
        trim_out = round(min(trim_in + desired, clip["duration_seconds"]), 2)
        if trim_out <= trim_in:
            trim_out = round(min(trim_in + 3.0, clip["duration_seconds"]), 2)

        transition = transition_cycle[i % len(transition_cycle)]
        td = 0.01 if transition == "hard_cut" else 0.5

        cuts.append({
            "order": i,
            "clip_id": clip["clip_id"],
            "trim_in": trim_in,
            "trim_out": trim_out,
            "transition": transition,
            "transition_duration_seconds": td,
            "scene": clip.get("scene", i + 1),
        })

    total = sum(c["trim_out"] - c["trim_in"] for c in cuts) - transition_overhead
    return {
        "version": "1.0",
        "cuts": cuts,
        "total_duration_seconds": round(max(total, 1.0), 2),
        "music_start_offset_seconds": 0.0,
    }


# ── Per-scene text timing ──────────────────────────────────────────────────────

def _compute_scene_text_timing(
    normalized_clips: list[dict],
    cuts_ordered: list[dict],
    scenes: list[dict],
) -> list[dict]:
    """Map each scene's overlay_text to its timestamp in the composed video."""
    scene_by_order = {cut["order"]: scenes[i] if i < len(scenes) else {} for i, cut in enumerate(cuts_ordered)}
    text_scenes = []
    current_time = 0.0

    for i, clip in enumerate(normalized_clips):
        dur = clip["duration_seconds"]
        td = clip.get("transition_duration", 0.5) if i < len(normalized_clips) - 1 else 0
        scene = scene_by_order.get(i, {})
        overlay = (scene.get("overlay_text") or "").strip()

        if overlay:
            margin = min(0.5, dur * 0.15)
            text_scenes.append({
                "text": overlay,
                "start": round(current_time + margin, 2),
                "end": round(current_time + dur - margin, 2),
                "position": "bottom",
            })

        current_time += dur - td

    return text_scenes


# ── Music resolver ─────────────────────────────────────────────────────────────

def _resolve_music(music_file: str | None) -> str | None:
    if not music_file:
        music_dir = MUSIC_LIBRARY_PATH
        if music_dir.exists():
            tracks = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.m4a")) + list(music_dir.glob("*.wav"))
            if tracks:
                return str(random.choice(tracks))
        return None
    path = MUSIC_LIBRARY_PATH / music_file
    return str(path) if path.exists() else None

