"""
Audio Clip pipeline — audio track drives everything:
  brief → analyze_audio → narrative_overlays (Claude) → clip_selection → APPROVAL
        → normalize → compose → text_overlay → mix_audio → deliver
The provided audio file becomes the primary audio track (original video audio is ducked).
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from backend.config import PROJECTS_DIR
from backend.job_manager import Job, JobStatus
from backend import settings_manager
from tools.analysis.clip_analyzer import ClipAnalyzer
from tools.audio.audio_mixer import AudioMixer
from tools.koofr.koofr_browser import KoofrBrowser
from tools.koofr.koofr_downloader import KoofrDownloader
from tools.video.text_renderer import TextRenderer
from tools.video.video_composer import VideoComposer
from tools.video.video_normalizer import VideoNormalizer


def run_audio_pipeline(job: Job, params: dict[str, Any]) -> None:
    project_id = f"audio-{uuid.uuid4().hex[:8]}"
    project_dir = (PROJECTS_DIR / project_id).resolve()
    clips_dir = project_dir / "clips"
    norm_dir = project_dir / "tmp" / "normalized"
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"
    for d in [clips_dir, norm_dir, output_dir]:
        d.mkdir(parents=True, exist_ok=True)

    try:
        # ── STAGE 1: brief ─────────────────────────────────────────────────────
        job.begin_stage("brief", "Processing Brief", "Interpreting your request...")
        job.update(progress_pct=3, status=JobStatus.RUNNING)

        prompt = params.get("prompt", "")
        audio_path = params.get("audio_path", "")
        koofr_folder = params.get("koofr_folder") or settings_manager.get_koofr_folder() or "/"

        if not audio_path or not Path(audio_path).exists():
            raise RuntimeError(f"Audio file not found: {audio_path}")

        audio_filename = Path(audio_path).name
        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: analyze audio + write narrative ───────────────────────────
        job.begin_stage("storyboard", "Analyzing Audio", f"Probing {audio_filename}...")
        job.update(progress_pct=10)

        audio_duration = _get_audio_duration(audio_path)

        job.update(progress_pct=18, message="Claude is writing narrative overlays...")
        scenes = _generate_audio_narrative(prompt, audio_duration, audio_filename)

        job.end_stage(
            "storyboard",
            f"{len(scenes)} scenes planned for {audio_duration:.1f}s of '{audio_filename}'",
        )

        # ── STAGE 3: clip selection ─────────────────────────────────────────────
        job.begin_stage("clip_selection", "Selecting Clips", "Scanning Koofr...")
        job.update(progress_pct=28)

        browser = KoofrBrowser()
        browse = browser.execute({"operation": "list_clips", "path": koofr_folder, "recursive": True})
        if not browse.success:
            raise RuntimeError(f"Koofr browse failed: {browse.error}")

        mount_id = browse.data["mount_id"]
        available = browse.data["clips"]
        if not available:
            raise RuntimeError(f"No clips found in Koofr folder '{koofr_folder}'")

        # Import fuzzy clip selector from the main pipeline
        from backend.pipeline_runner import _select_clips_for_scenes
        pool = available.copy()
        random.shuffle(pool)
        selected = _select_clips_for_scenes(scenes, pool)

        # Parallel download + analyze
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
            an = ClipAnalyzer().execute({"local_path": dl.data["local_path"]})
            if not an.success:
                return i, None, f"Could not analyze {kclip['name']}"
            return i, {
                "clip_id": f"clip_{i:03d}",
                "filename": kclip["name"],
                "local_path": dl.data["local_path"],
                "duration_seconds": float(an.data["duration_seconds"]),
                "resolution": {"width": an.data["width"], "height": an.data["height"]},
                "fps": an.data["fps"],
            }, None

        clip_entries_raw: dict[int, dict] = {}
        n_dl = len(selected)
        dl_done = 0
        with ThreadPoolExecutor(max_workers=4) as pool_ex:
            futures = {pool_ex.submit(_dl, (i, kc)): i for i, kc in enumerate(selected)}
            for fut in as_completed(futures):
                idx, entry, err = fut.result()
                dl_done += 1
                if err:
                    job.update(message=f"Warning: {err} ({dl_done}/{n_dl})")
                else:
                    clip_entries_raw[idx] = entry
                    job.update(
                        progress_pct=28 + int((dl_done / n_dl) * 12),
                        message=f"Downloaded {dl_done}/{n_dl} clips",
                    )

        clip_entries = [clip_entries_raw[i] for i in sorted(clip_entries_raw)]
        if not clip_entries:
            raise RuntimeError("No clips could be downloaded from Koofr")

        job.end_stage("clip_selection", f"{len(clip_entries)} clips ready")

        # ── APPROVAL ───────────────────────────────────────────────────────────
        job.request_approval({
            "module": "audio",
            "storyboard": {
                "theme": prompt or audio_filename,
                "scenes": scenes,
            },
            "reel_summary": {
                "mode": "audio",
                "prompt": prompt,
                "target_duration_seconds": round(audio_duration, 1),
                "scene_count": len(scenes),
                "music_file": audio_filename,
            },
        })
        approved = job.wait_for_approval(timeout=1800)
        if not approved:
            raise RuntimeError("Approval timed out after 30 minutes")

        # Merge user edits back into scenes
        resp_scenes = (job.approval_response or {}).get("scenes", [])
        edited = {s["scene"]: s for s in resp_scenes if "scene" in s}
        for s in scenes:
            ed = edited.get(s["scene"], {})
            if "overlay_text" in ed:
                s["overlay_text"] = ed["overlay_text"]

        # ── STAGE 4: compose ────────────────────────────────────────────────────
        job.begin_stage("compose", "Composing Video", "Normalizing clips in parallel...")
        job.update(progress_pct=52)

        n_clips = len(clip_entries)
        dur_per = audio_duration / n_clips
        norm_done = 0

        def _norm(args):
            i, clip = args
            out = str(norm_dir / f"{i:02d}_{clip['clip_id']}.mp4")
            # If clip is much longer than needed, start 15% in to avoid slow intros
            trim_in = 0.0
            if clip["duration_seconds"] > dur_per * 3:
                trim_in = round(clip["duration_seconds"] * 0.15, 2)
            trim_out = round(min(trim_in + dur_per, clip["duration_seconds"]), 2)
            if trim_out <= trim_in:
                trim_out = round(min(trim_in + 3.0, clip["duration_seconds"]), 2)
            r = VideoNormalizer().execute({
                "input_path": clip["local_path"],
                "output_path": out,
                "trim_in": trim_in,
                "trim_out": trim_out,
            })
            return i, out, trim_out - trim_in, r

        norm_map: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=4) as pool_ex:
            futures = {pool_ex.submit(_norm, (i, c)): i for i, c in enumerate(clip_entries)}
            for fut in as_completed(futures):
                i, out, actual_dur, r = fut.result()
                norm_done += 1
                if not r.success:
                    raise RuntimeError(f"Normalize failed: {r.error}")
                norm_map[i] = {
                    "path": out,
                    "duration_seconds": actual_dur,
                    "transition": "hard_cut",
                    "transition_duration": 0.01,
                }
                job.update(
                    progress_pct=52 + int((norm_done / n_clips) * 18),
                    message=f"Normalized {norm_done}/{n_clips} clips",
                )

        normalized_clips = [norm_map[i] for i in sorted(norm_map)]

        job.update(progress_pct=72, message="Joining clips...")
        composed_path = str(tmp_dir / "composed.mp4")
        compose = VideoComposer().execute({"clips": normalized_clips, "output_path": composed_path})
        if not compose.success:
            raise RuntimeError(f"Compose failed: {compose.error}")

        # Per-scene text overlays timed to each clip
        job.update(progress_pct=80, message="Rendering text overlays...")
        from backend.pipeline_runner import _compute_scene_text_timing

        fake_cuts = [{"order": i, "scene": i + 1} for i in range(len(normalized_clips))]
        scene_text = _compute_scene_text_timing(normalized_clips, fake_cuts, scenes)

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
                job.update(message=f"Warning: text overlay failed: {tr.error}")

        # Mix the trending audio as the primary track (duck original)
        job.update(progress_pct=88, message="Mixing trending audio...")
        final_path = str(output_dir / "audio_clip_final.mp4")
        mix = AudioMixer().execute({
            "video_path": titled_path,
            "output_path": final_path,
            "voiceover_path": None,
            "music_path": audio_path,
            "duck_original": True,
            "original_volume": 0.0,   # fully mute original video sound
            "music_volume": 0.95,      # trending audio is the star
        })
        if not mix.success:
            raise RuntimeError(f"Audio mix failed: {mix.error}")

        job.end_stage("compose", "Video composed!")

        # ── STAGE 5: deliver ────────────────────────────────────────────────────
        job.begin_stage("deliver", "Ready!", "Your audio clip is ready")
        job.update(progress_pct=99)

        file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0
        local_copy_path = _copy_to_local_output(project_dir, project_id)
        job.end_stage("deliver", "Done")
        job.update(
            status=JobStatus.COMPLETED,
            progress_pct=100,
            message="Audio clip ready!" + (f" Saved to {local_copy_path}" if local_copy_path else ""),
            result={
                "project_id": project_id,
                "output_path": final_path,
                "local_copy_path": local_copy_path,
                "duration_seconds": audio_duration,
                "file_size_bytes": file_size,
                "clips_used": len(clip_entries),
                "voiceover": False,
                "audio_file": audio_filename,
            },
        )

    except Exception as exc:
        job.update(status=JobStatus.FAILED, message="Audio pipeline failed", error=str(exc))
        raise


# ── Audio analysis ─────────────────────────────────────────────────────────────

def _get_audio_duration(path: str) -> float:
    r = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 30.0


# ── Narrative generation ───────────────────────────────────────────────────────

def _generate_audio_narrative(prompt: str, duration: float, audio_filename: str) -> list[dict]:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")
    brand_voice = settings_manager.get_brand_voice()
    context = f"\n\nBrand voice:\n{brand_voice[:300]}" if brand_voice else ""

    n_scenes = max(3, min(8, int(duration / 5)))
    dur_per = round(duration / n_scenes, 1)

    if not api_key:
        return _default_audio_scenes(n_scenes, dur_per, prompt)

    system = """You are an Instagram Reels director writing on-screen text overlays for a video synced to trending audio.

FRAMEWORK — ABT arc across scenes:
- AND scenes: establish the world, identity match, relatable situation
- BUT scenes: the tension, the open loop, the contradiction — this is where retention is won
- THEREFORE scenes: the payoff, the reveal, the earned resolution

OVERLAY TEXT RULES:
- Max 8 words per line — punchy, not descriptive
- Front-load tension — lead with the interesting thing
- Say "you" not "people" or "everyone"
- Match the audio's energy — high energy audio = sharp short lines, emotional audio = resonant lines
- Each overlay must flow as one cohesive narrative arc when read in sequence
- Add a re-hook line roughly every 2–3 scenes ("and here's the part nobody talks about…")

The audio track sets the emotional tone — write overlays that SYNC with that energy, not just describe the visuals.
Respond ONLY with valid JSON — no markdown fences."""

    user = (
        f"Write {n_scenes} ABT-structured on-screen text overlays for a video using this audio: '{audio_filename}'\n"
        f"Narrative prompt: {prompt or 'engaging visual story'}{context}\n"
        f"Total duration: {duration:.1f}s (~{dur_per}s per scene)\n\n"
        "Arc the overlays as: hook tension (scene 1) → setup/And (scenes 2-3) → "
        "conflict/But (middle scenes) → resolution/Therefore (final scenes).\n\n"
        'Return JSON: {"theme": "one-line theme", "scenes": [{'
        '"scene": 1, "role": "hook|and|but|therefore|trigger", '
        '"visual_description": "specific visual direction for this scene", '
        '"clip_hint": "2-4 specific keywords to find a matching clip", '
        f'"duration_seconds": {dur_per}, '
        '"overlay_text": "Punchy on-screen text max 8 words — front-load tension", '
        '"voiceover": ""'
        "}]}"
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
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _default_audio_scenes(n_scenes, dur_per, prompt)
    scenes = data.get("scenes", [])
    if not scenes:
        return _default_audio_scenes(n_scenes, dur_per, prompt)
    # Normalize durations to sum to audio_duration
    total = sum(s.get("duration_seconds", dur_per) for s in scenes)
    scale = duration / max(total, 1)
    for s in scenes:
        s["duration_seconds"] = round(s.get("duration_seconds", dur_per) * scale, 1)
    return scenes


def _default_audio_scenes(n: int, dur_per: float, prompt: str) -> list[dict]:
    roles = ["opening", "build-up", "peak", "build-up", "peak", "outro"]
    return [
        {
            "scene": i + 1,
            "role": roles[i % len(roles)],
            "visual_description": "Compelling visual moment",
            "clip_hint": prompt or "",
            "duration_seconds": dur_per,
            "overlay_text": "",
            "voiceover": "",
        }
        for i in range(n)
    ]


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
