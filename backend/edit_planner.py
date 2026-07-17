"""
Retention-aware edit planner.

Implements:
  - ABT role-weighted scene durations (But gets the most time — that's where retention is won)
  - Valley fix: ensures "But" arrives within 7–10s to catch falling viewers
  - Pattern interrupts: hard cuts every 8–10s to reset the attention clock
  - Role-appropriate transitions (hard cut into But, dissolve into Therefore)
  - Smart voiceover positioning: each scene's VO starts at its exact scene timestamp
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# How much of total usable time each ABT role gets
_ROLE_WEIGHTS: dict[str, float] = {
    "hook":      0.10,   # ~3s in a 30s reel — snap in fast
    "and":       0.13,   # ~4s — identity match, keep tight
    "but":       0.34,   # ~10s — open loop, this is where retention is won
    "therefore": 0.29,   # ~9s — payoff, earned resolution
    "trigger":   0.14,   # ~4s — share trigger / CTA
}

# Transition type per role
# "but" uses hard_cut — the jarring cut IS the pattern interrupt
_ROLE_TRANSITIONS: dict[str, tuple[str, float]] = {
    "hook":      ("hard_cut",      0.01),
    "and":       ("cross_dissolve", 0.40),
    "but":       ("hard_cut",      0.01),   # Valley fix — pattern interrupt
    "therefore": ("cross_dissolve", 0.50),
    "trigger":   ("hard_cut",      0.01),
}

VALLEY_WINDOW_START_S = 7.0    # "But" must start no later than this
VALLEY_WINDOW_END_S   = 10.0
PATTERN_INTERRUPT_EVERY_S = 9.0  # force a hard cut at this interval


def plan_edit(
    clip_manifest: dict,
    scenes: list[dict],
    target_duration: float,
    scene_vo_durations: dict[int, float] | None = None,
) -> dict:
    """
    Return edit_decisions with timing and pattern interrupts.

    When scene_vo_durations is provided, each clip is trimmed exactly to its
    voiceover duration — the script drives the edit. Scenes without a VO fall
    back to ABT role-weighted timing. The Valley Fix is skipped when VOs are
    present because the scriptwriter is trusted to pace the "But" correctly.
    """
    clips = clip_manifest["clips"]
    n = len(clips)
    transition_overhead = (n - 1) * 0.4
    usable = max(target_duration - transition_overhead, n * 2.5)

    scene_roles = [_norm_role(s.get("role", "and")) for s in scenes]

    # --- 1. Build per-scene desired duration ---
    # When VO durations are known, use them directly; otherwise use ABT weights.
    raw_w = [_ROLE_WEIGHTS.get(r, _ROLE_WEIGHTS["and"]) for r in scene_roles]
    total_w = sum(raw_w)
    vo_map = scene_vo_durations or {}

    scene_durations: list[float] = []
    for i, clip in enumerate(clips):
        sn = clip.get("scene") or (i + 1)
        vo_dur = vo_map.get(sn, 0.0)
        if vo_dur > 0:
            scene_durations.append(vo_dur)
        else:
            role = scene_roles[i] if i < len(scene_roles) else "and"
            w = _ROLE_WEIGHTS.get(role, _ROLE_WEIGHTS["and"])
            scene_durations.append(w / total_w * usable)

    # --- 2. Valley fix: only when script doesn't already control timing ---
    but_idx = next((i for i, r in enumerate(scene_roles) if r == "but"), None)
    if but_idx is not None and not scene_vo_durations:
        time_to_but = sum(scene_durations[:but_idx])
        if time_to_but > VALLEY_WINDOW_END_S:
            budget = VALLEY_WINDOW_END_S - 0.5
            scale = budget / max(time_to_but, 0.01)
            freed = 0.0
            for i in range(but_idx):
                new_dur = max(2.0, scene_durations[i] * scale)
                freed += scene_durations[i] - new_dur
                scene_durations[i] = new_dur
            post = n - but_idx
            if post > 0:
                for i in range(but_idx, n):
                    scene_durations[i] += freed / post

    # --- 3. Build cuts with role-appropriate transitions + pattern interrupts ---
    cuts = []
    abs_time = 0.0
    last_interrupt_at = 0.0

    for i, clip in enumerate(clips):
        desired = round(scene_durations[i] if i < len(scene_durations) else usable / n, 2)

        # Pick the start offset into the clip (skip slow intros for long clips)
        trim_in = 0.0
        if clip["duration_seconds"] > desired * 3:
            trim_in = round(clip["duration_seconds"] * 0.2, 2)
        trim_out = round(min(trim_in + desired, clip["duration_seconds"]), 2)
        if trim_out <= trim_in:
            trim_out = round(min(trim_in + 3.0, clip["duration_seconds"]), 2)

        role = scene_roles[i] if i < len(scene_roles) else "and"
        transition, td = _ROLE_TRANSITIONS.get(role, ("cross_dissolve", 0.5))

        # Pattern interrupt override: if it's been 8–10s since the last hard cut,
        # force a hard_cut here regardless of role (skip the first cut)
        if i > 0 and transition != "hard_cut":
            if abs_time - last_interrupt_at >= PATTERN_INTERRUPT_EVERY_S:
                transition = "hard_cut"
                td = 0.01

        if transition == "hard_cut":
            last_interrupt_at = abs_time

        cuts.append({
            "order": i,
            "clip_id": clip["clip_id"],
            "trim_in": trim_in,
            "trim_out": trim_out,
            "transition": transition,
            "transition_duration_seconds": td,
            "scene": clip.get("scene") or (i + 1),
        })

        abs_time += (trim_out - trim_in) - td

    total = sum(c["trim_out"] - c["trim_in"] for c in cuts) - transition_overhead

    return {
        "version": "1.1",
        "cuts": cuts,
        "total_duration_seconds": round(max(total, 1.0), 2),
        "music_start_offset_seconds": 0.0,
        "abt_timing": _abt_timing_report(cuts, scenes),
    }


def build_positioned_voiceover(
    scene_vo_files: dict[int, str],   # {scene_number: mp3_path}
    normalized_clips: list[dict],     # from composition — has actual durations
    cuts_ordered: list[dict],         # sorted cuts from edit_decisions
    voiceover_dir: Path,
    total_duration: float,
) -> str | None:
    """
    Build a time-positioned voiceover track where each scene's speech starts
    at that scene's actual timestamp in the composed video.

    This replaces naive back-to-back concatenation — voiceover now syncs
    to the visual edit rather than running ahead of it.
    """
    # Step 1: calculate start time AND clip duration for each cut position
    cut_start_times: list[float] = []
    cut_clip_durations: list[float] = []
    current_time = 0.0
    for i, clip in enumerate(normalized_clips):
        cut_start_times.append(current_time)
        cut_clip_durations.append(clip["duration_seconds"])
        dur = clip["duration_seconds"]
        td = clip.get("transition_duration", 0.4) if i < len(normalized_clips) - 1 else 0.0
        current_time += dur - td

    # Step 2: group cuts BY SCENE — a scene may span several clips (multi-clip, when
    # its narration is longer than one model clip). Each scene's start = its FIRST
    # cut's start; its available VO window = the SUM of all its cuts' durations. This
    # is what stops the voiceover from being chopped: the window is the scene's whole
    # visual span, not just one clip. For one-clip-per-scene (e.g. Koofr) this is
    # identical to the old per-clip behaviour.
    scene_start_times: dict[int, float] = {}
    scene_total_durations: dict[int, float] = {}
    for i, cut in enumerate(cuts_ordered):
        if i < len(cut_start_times):
            scene_num = cut.get("scene") or (i + 1)
            if scene_num not in scene_start_times:
                scene_start_times[scene_num] = round(cut_start_times[i], 3)
                scene_total_durations[scene_num] = 0.0
            scene_total_durations[scene_num] += cut_clip_durations[i]

    # Step 3: build (path, delay_ms, max_dur) — skip any VO whose scene has no clip.
    # Never default to delay=0: that causes all missing-scene VOs to overlap scene 1.
    # max_dur = the scene's full visual span, so speech only bleeds if it genuinely
    # exceeds the visuals allotted to that scene (with multi-clip it no longer does).
    valid: list[tuple[str, int, float]] = []  # (path, delay_ms, max_dur)
    for scene_num, vo_path in sorted(scene_vo_files.items()):
        if not Path(vo_path).exists():
            continue
        if scene_num not in scene_start_times:
            continue  # clip for this scene is missing — skip rather than play at t=0
        delay_ms = int(scene_start_times[scene_num] * 1000)
        max_dur = scene_total_durations[scene_num]
        valid.append((vo_path, delay_ms, max_dur))

    if not valid:
        return None

    out_path = str(voiceover_dir / "voiceover_positioned.mp3")

    if len(valid) == 1:
        path, delay_ms, max_dur = valid[0]
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", path,
             "-af", f"atrim=duration={max_dur},adelay={delay_ms}|{delay_ms},apad=whole_dur={total_duration}",
             "-t", str(total_duration),
             "-c:a", "libmp3lame", "-q:a", "2",
             out_path],
            capture_output=True, text=True, timeout=120,
        )
        return out_path if r.returncode == 0 else path

    # Multiple scenes — trim each VO to its clip duration, then delay and pad.
    # atrim BEFORE adelay ensures speech doesn't bleed into the next scene.
    # apad=whole_dur ensures amix sees the full video length (not just raw TTS length).
    inputs: list[str] = []
    filter_parts: list[str] = []
    for idx, (path, delay_ms, max_dur) in enumerate(valid):
        inputs += ["-i", path]
        filter_parts.append(
            f"[{idx}:a]atrim=duration={max_dur},"
            f"adelay={delay_ms}|{delay_ms},"
            f"apad=whole_dur={total_duration}[a{idx}]"
        )

    n = len(valid)
    mix = "".join(f"[a{i}]" for i in range(n))
    filter_complex = (
        ";".join(filter_parts) + ";"
        f"{mix}amix=inputs={n}:duration=longest:normalize=0[out]"
    )

    r = subprocess.run(
        ["ffmpeg", "-y", *inputs,
         "-filter_complex", filter_complex,
         "-map", "[out]",
         "-t", str(total_duration),
         "-c:a", "libmp3lame", "-q:a", "2",
         out_path],
        capture_output=True, text=True, timeout=120,
    )
    return out_path if r.returncode == 0 else valid[0][0]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _norm_role(role: str) -> str:
    role = role.lower().strip()
    # Map common variations
    mapping = {
        "opening": "hook", "intro": "hook",
        "build-up": "and", "build_up": "and", "setup": "and",
        "peak": "but", "conflict": "but", "tension": "but",
        "resolution": "therefore", "outro": "trigger", "cta": "trigger",
    }
    return mapping.get(role, role if role in _ROLE_WEIGHTS else "and")


def _abt_timing_report(cuts: list[dict], scenes: list[dict]) -> dict:
    """Returns when each ABT role starts (seconds) — for diagnostics."""
    t = 0.0
    report: dict[str, float] = {}
    for i, cut in enumerate(cuts):
        dur = cut["trim_out"] - cut["trim_in"]
        td = cut.get("transition_duration_seconds", 0.4)
        if i < len(scenes):
            role = scenes[i].get("role", "?")
            if role not in report:
                report[role] = round(t, 1)
        t += dur - (td if i < len(cuts) - 1 else 0)
    return report
