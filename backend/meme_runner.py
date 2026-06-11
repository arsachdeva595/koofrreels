"""
Meme Clip pipeline:
  brief → clip_fetch → meme_text (Claude) → APPROVAL → normalize → text_overlay → audio_mix → deliver
One clip, two overlay lines (top = setup, bottom = punchline).
"""
from __future__ import annotations

import json
import os
import random
import shutil
import uuid
from pathlib import Path
from typing import Any

from backend.config import MUSIC_LIBRARY_PATH, PROJECTS_DIR
from backend.job_manager import Job, JobStatus
from backend import settings_manager
from tools.analysis.clip_analyzer import ClipAnalyzer
from tools.audio.audio_mixer import AudioMixer
from tools.koofr.koofr_browser import KoofrBrowser
from tools.koofr.koofr_downloader import KoofrDownloader
from tools.video.text_renderer import TextRenderer
from tools.video.video_normalizer import VideoNormalizer


def run_meme_pipeline(job: Job, params: dict[str, Any]) -> None:
    project_id = f"meme-{uuid.uuid4().hex[:8]}"
    project_dir = (PROJECTS_DIR / project_id).resolve()
    clips_dir = project_dir / "clips"
    tmp_dir = project_dir / "tmp"
    output_dir = project_dir / "output"
    for d in [clips_dir, tmp_dir, output_dir]:
        d.mkdir(parents=True, exist_ok=True)

    try:
        # ── STAGE 1: brief ─────────────────────────────────────────────────────
        job.begin_stage("brief", "Processing Brief", "Interpreting your meme request...")
        job.update(progress_pct=5, status=JobStatus.RUNNING)

        prompt = params.get("prompt", "something funny")
        clip_id = params.get("clip_id")
        koofr_folder = params.get("koofr_folder") or settings_manager.get_koofr_folder() or "/"
        target_duration = float(params.get("target_duration_seconds", 8.0))
        music_file = params.get("music_file")

        job.end_stage("brief", "Brief ready")

        # ── STAGE 2: fetch one clip ─────────────────────────────────────────────
        job.begin_stage("clip_selection", "Fetching Clip", "Picking a clip from Koofr...")
        job.update(progress_pct=15)

        browser = KoofrBrowser()
        browse = browser.execute({"operation": "list_clips", "path": koofr_folder, "recursive": True})
        if not browse.success:
            raise RuntimeError(f"Koofr browse failed: {browse.error}")

        mount_id = browse.data["mount_id"]
        available = browse.data["clips"]
        if not available:
            raise RuntimeError(f"No clips found in Koofr folder '{koofr_folder}'")

        if clip_id:
            chosen = next((c for c in available if c["file_id"] == clip_id), available[0])
        else:
            chosen = random.choice(available)

        dl = KoofrDownloader().execute({
            "mount_id": mount_id,
            "file_path": chosen["path"],
            "dest_dir": str(clips_dir),
            "filename": chosen["name"],
        })
        if not dl.success:
            raise RuntimeError(f"Download failed: {dl.error}")

        analysis = ClipAnalyzer().execute({"local_path": dl.data["local_path"]})
        if not analysis.success:
            raise RuntimeError(f"Clip analysis failed: {analysis.error}")

        clip_local_path = dl.data["local_path"]
        clip_duration = min(float(analysis.data["duration_seconds"]), target_duration)

        job.end_stage("clip_selection", f"Got: {chosen['name']}")

        # ── STAGE 3: generate meme text ────────────────────────────────────────
        job.begin_stage("storyboard", "Generating Meme Text", "Claude is writing your meme...")
        job.update(progress_pct=40)

        line1, line2 = _generate_meme_text(prompt)

        job.end_stage("storyboard", f'"{line1}" / "{line2}"')

        # ── APPROVAL ───────────────────────────────────────────────────────────
        job.request_approval({
            "module": "meme",
            "storyboard": {
                "theme": f"Meme: {prompt}",
                "scenes": [
                    {
                        "scene": 1,
                        "role": "top-text",
                        "visual_description": "Top line — setup / context",
                        "clip_hint": "",
                        "duration_seconds": round(clip_duration, 1),
                        "overlay_text": line1,
                        "voiceover": "",
                    },
                    {
                        "scene": 2,
                        "role": "bottom-text",
                        "visual_description": "Bottom line — punchline",
                        "clip_hint": "",
                        "duration_seconds": round(clip_duration, 1),
                        "overlay_text": line2,
                        "voiceover": "",
                    },
                ],
            },
            "reel_summary": {
                "mode": "meme",
                "prompt": prompt,
                "target_duration_seconds": round(clip_duration, 1),
                "scene_count": 2,
                "clip": chosen["name"],
                "music_file": music_file or "Random from library",
            },
        })
        approved = job.wait_for_approval(timeout=1800)
        if not approved:
            raise RuntimeError("Approval timed out after 30 minutes")

        # Merge user edits (scene 1 = line1, scene 2 = line2)
        resp_scenes = (job.approval_response or {}).get("scenes", [])
        edited = {s["scene"]: s for s in resp_scenes if "scene" in s}
        line1 = (edited.get(1, {}).get("overlay_text") or line1).strip() or line1
        line2 = (edited.get(2, {}).get("overlay_text") or line2).strip() or line2

        # ── STAGE 4: normalize clip ────────────────────────────────────────────
        job.begin_stage("compose", "Rendering Meme", "Normalizing clip...")
        job.update(progress_pct=58)

        norm_path = str(tmp_dir / "normalized.mp4")
        norm = VideoNormalizer().execute({
            "input_path": clip_local_path,
            "output_path": norm_path,
            "trim_in": 0.0,
            "trim_out": clip_duration,
        })
        if not norm.success:
            raise RuntimeError(f"Normalize failed: {norm.error}")

        # ── STAGE 5: burn meme text ────────────────────────────────────────────
        job.update(progress_pct=72, message="Burning meme text...")

        margin = min(0.5, clip_duration * 0.1)
        text_scenes = []
        if line1:
            text_scenes.append({
                "text": line1,
                "start": margin,
                "end": clip_duration - margin,
                "position": "top",
            })
        if line2:
            text_scenes.append({
                "text": line2,
                "start": margin,
                "end": clip_duration - margin,
                "position": "bottom",
            })

        titled_path = norm_path
        if text_scenes:
            titled_path = str(tmp_dir / "meme_text.mp4")
            tr = TextRenderer().execute({
                "input_path": norm_path,
                "output_path": titled_path,
                "scenes": text_scenes,
                "font_size": 78,
            })
            if not tr.success:
                titled_path = norm_path
                job.update(message=f"Warning: text overlay failed: {tr.error}")

        # ── STAGE 6: audio mix ─────────────────────────────────────────────────
        job.update(progress_pct=85, message="Mixing audio...")
        final_path = str(output_dir / "meme_final.mp4")
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

        job.end_stage("compose", "Meme rendered!")

        # ── STAGE 7: deliver ───────────────────────────────────────────────────
        job.begin_stage("deliver", "Ready!", "Your meme clip is ready")
        job.update(progress_pct=99)

        file_size = Path(final_path).stat().st_size if Path(final_path).exists() else 0
        local_copy_path = _copy_to_local_output(project_dir, project_id)
        job.end_stage("deliver", "Done")
        job.update(
            status=JobStatus.COMPLETED,
            progress_pct=100,
            message="Meme clip ready!" + (f" Saved to {local_copy_path}" if local_copy_path else ""),
            result={
                "project_id": project_id,
                "output_path": final_path,
                "local_copy_path": local_copy_path,
                "duration_seconds": clip_duration,
                "file_size_bytes": file_size,
                "clips_used": 1,
                "voiceover": False,
                "meme_text": {"line1": line1, "line2": line2},
            },
        )

    except Exception as exc:
        job.update(status=JobStatus.FAILED, message="Meme pipeline failed", error=str(exc))
        raise


# ── Meme text generation ───────────────────────────────────────────────────────

def _generate_meme_text(prompt: str) -> tuple[str, str]:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")
    master_prompt = settings_manager.get("meme_master_prompt", "")

    default_system = (
        "You are a meme writer for Instagram Reels. You apply the ABT framework in 2 lines:\n"
        "line1 (top — the BUT): the tension, the relatable frustration, the open loop. "
        "This is what makes the viewer lean in. Max 10 words. Say 'you' not 'people'. "
        "Front-load the tension — do NOT start with setup or context.\n"
        "line2 (bottom — the THEREFORE): the punchline, the payoff, the earned resolution. "
        "Must feel like a genuine unlock, not a generic punchline. Max 10 words.\n"
        "The two lines together must create an open loop (line1) and close it (line2). "
        "Never use apostrophes in the text. "
        'Respond ONLY with valid JSON: {"line1": "tension line here", "line2": "payoff line here"}'
    )
    system = master_prompt.strip() if master_prompt.strip() else default_system

    if not api_key:
        return ("When you see this...", "You know this is real")

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Write meme text for this clip: {prompt}"}],
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
        return ("When you see this...", "You know this is real")
    return (
        data.get("line1", "When you see this..."),
        data.get("line2", "You know this is real"),
    )


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
