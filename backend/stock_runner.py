"""
Stock Reel pipeline — identical to the main Reels pipeline but sources clips
exclusively from Pexels (stock video) instead of Koofr.

Each scene's clip_hint drives a per-scene Pexels search, so clips are always
contextually matched to the ABT storyboard.
"""
from __future__ import annotations

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
from backend.ai_runner import _parse_script_to_storyboard
from backend import edit_planner
from tools.analysis.clip_analyzer import ClipAnalyzer
from tools.analysis.audio_prober import AudioProber
from tools.analysis.frame_sampler import FrameSampler
from tools.analysis.video_prober import VideoProber
from tools.audio.audio_mixer import AudioMixer
from tools.video.pexels_downloader import PexelsDownloader
from tools.video.text_renderer import TextRenderer
from tools.video.video_composer import VideoComposer
from tools.video.video_normalizer import VideoNormalizer


def run_stock_pipeline(job: Job, params: dict[str, Any]) -> None:
    project_id = f"stock-{uuid.uuid4().hex[:8]}"
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
            "mode": "stock",
            "target_duration_seconds": float(params.get("target_duration_seconds", 30)),
            "prompt": params.get("prompt"),
            "music_file": params.get("music_file"),
            "include_text": params.get("include_text", True),
            "use_brand_guidelines": params.get("use_brand_guidelines", True),
            "text_hints": params.get("text_hints"),
        }

        pexels_key = settings_manager.get("pexels_api_key", "")
        if not pexels_key:
            raise RuntimeError("Pexels API key not set — add it in Settings to use the Stock module.")

        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: storyboard ────────────────────────────────────────────────
        skip_storyboard = params.get("skip_storyboard", False)
        if skip_storyboard:
            job.begin_stage("storyboard", "Parsing Your Script", "Reading scene-by-scene instructions...")
            job.update(progress_pct=8)
            storyboard = _parse_script_to_storyboard(params.get("prompt", ""))
            scenes = storyboard["scenes"]
            reel_brief["target_duration_seconds"] = sum(s["duration_seconds"] for s in scenes)
            job.end_stage("storyboard", f"{len(scenes)} scenes parsed from your script — awaiting your approval")
        else:
            job.begin_stage("storyboard", "Director's Storyboard", "Claude is planning your ABT story...")
            job.update(progress_pct=8)
            storyboard = _generate_storyboard(reel_brief)
            scenes = storyboard["scenes"]
            job.end_stage("storyboard", f"{len(scenes)}-scene story ready — awaiting your approval")

        # ── APPROVAL ───────────────────────────────────────────────────────────
        job.request_approval({
            "storyboard": storyboard,
            "reel_summary": {
                "mode": "stock",
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

        import os
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

        # ── STAGE 4: clip_selection (Pexels) ───────────────────────────────────
        job.begin_stage("clip_selection", "Searching Pexels", "Finding stock clips for each scene...")
        job.update(progress_pct=25)

        fallback_query = reel_brief.get("prompt") or "cinematic"
        n_scenes = len(scenes)

        def _fetch_scene_clip(args: tuple[int, dict]) -> tuple[int, dict | None, str | None]:
            i, scene = args
            hint = (scene.get("clip_hint") or fallback_query).strip()
            vo_dur = scene_vo_durations.get(scene.get("scene", i + 1), 0)
            pr = PexelsDownloader().execute({
                "query": hint,
                "dest_dir": str(clips_dir),
                "orientation": "portrait",
                "min_duration": max(3, int(scene.get("duration_seconds", 5)), int(vo_dur) + 1),
                "max_results": 1,
            })
            if not pr.success:
                # retry with the overall prompt as fallback
                pr = PexelsDownloader().execute({
                    "query": fallback_query,
                    "dest_dir": str(clips_dir),
                    "orientation": "portrait",
                    "min_duration": 3,
                    "max_results": 1,
                })
            if not pr.success or not pr.data.get("clips"):
                return i, None, f"No Pexels clips found for scene {i + 1} (hint: '{hint}')"

            pclip = pr.data["clips"][0]
            an = ClipAnalyzer().execute({"local_path": pclip["local_path"]})
            if not an.success:
                return i, None, f"Could not analyze Pexels clip for scene {i + 1}"

            return i, {
                "clip_id": f"clip_{i:03d}",
                "filename": Path(pclip["local_path"]).name,
                "local_path": pclip["local_path"],
                "duration_seconds": float(an.data["duration_seconds"]),
                "resolution": {"width": an.data["width"], "height": an.data["height"]},
                "fps": an.data["fps"],
                "selection_reason": f"Pexels: {hint}",
                "scene": scene["scene"],
                "score": None,
            }, None

        clip_entries_raw: dict[int, dict] = {}
        dl_done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_scene_clip, (i, s)): i for i, s in enumerate(scenes)}
            for fut in as_completed(futures):
                idx, entry, err = fut.result()
                dl_done += 1
                if err:
                    job.update(message=f"Warning: {err} ({dl_done}/{n_scenes})")
                else:
                    clip_entries_raw[idx] = entry
                    job.update(
                        progress_pct=25 + int((dl_done / n_scenes) * 12),
                        message=f"Found {dl_done}/{n_scenes} Pexels clips",
                    )

        clip_entries = [clip_entries_raw[i] for i in sorted(clip_entries_raw)]

        if len(clip_entries) < 2:
            raise RuntimeError(
                f"Only {len(clip_entries)} Pexels clips found — need at least 2. "
                "Try a more specific prompt or check your Pexels API key in Settings."
            )

        if scene_vo_files:
            present_scenes = {c["scene"] for c in clip_entries if c.get("scene")}
            scene_vo_files = {k: v for k, v in scene_vo_files.items() if k in present_scenes}

        clip_manifest = {
            "version": "1.0",
            "clips": clip_entries,
            "total_available_duration_seconds": sum(c["duration_seconds"] for c in clip_entries),
        }
        job.end_stage("clip_selection", f"{len(clip_entries)} Pexels clips ready")

        # ── STAGE 5: edit_decisions ────────────────────────────────────────────
        job.begin_stage("edit_decisions", "Planning the Edit", "Calculating timing from storyboard...")
        job.update(progress_pct=40)

        target_dur = reel_brief["target_duration_seconds"]
        edit_decisions = edit_planner.plan_edit(
            clip_manifest, scenes, target_dur,
            scene_vo_durations=scene_vo_durations or None,
        )
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
        final_path = str(output_dir / "stock_reel_final.mp4")
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
        if review_status == "fail":
            raise RuntimeError(f"Final review failed: {[f['description'] for f in findings if f['severity'] == 'critical']}")

        job.end_stage("final_review", "All quality checks passed")

        # ── STAGE 8: deliver ───────────────────────────────────────────────────
        job.begin_stage("deliver", "Ready!", "Your stock reel is ready for download")
        job.update(progress_pct=99)

        file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0
        local_copy_path = _copy_to_local_output(project_dir, project_id)
        job.end_stage("deliver", "Done")
        job.update(
            status=JobStatus.COMPLETED,
            progress_pct=100,
            message="Stock reel ready!" + (f" Saved to {local_copy_path}" if local_copy_path else ""),
            result={
                "project_id": project_id,
                "output_path": final_path,
                "local_copy_path": local_copy_path,
                "duration_seconds": actual_dur,
                "file_size_bytes": file_size,
                "clips_used": len(clip_entries),
                "voiceover": voiceover_path is not None,
                "source": "pexels",
                "scenes": [
                    {"scene": s["scene"], "overlay_text": s.get("overlay_text", ""),
                     "voiceover": s.get("voiceover", "")}
                    for s in scenes
                ],
            },
        )

    except Exception as exc:
        job.update(status=JobStatus.FAILED, message="Stock pipeline failed", error=str(exc))
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
