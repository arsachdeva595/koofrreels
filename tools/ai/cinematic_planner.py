"""
Cinematic render planner — the brain of the pause-driven edit.

Given the director's shots and the speech beats detected in each shot's voiceover
(see tools/audio/pause_detector), it produces an ordered list of *render units*.
Each unit is one clip to generate, tagged with which keyframe still it needs and how
long it should play. Cuts land on the pauses between beats; long beats fan out into
several clips (Veo3's 8s cap); extra beats within a shot reuse the shot's content as
"continuation" stills so the look stays coherent.

Pure and deterministic — no I/O — so the pipeline can plan the whole timeline (and its
cost) before generating a single still or clip.
"""
from __future__ import annotations

from tools.audio.pause_detector import plan_beat_clips


def build_render_plan(
    shots: list[dict],
    shot_beats: dict[int, list[dict]],
    max_clip_seconds: float = 8.0,
    default_shot_seconds: float = 5.0,
) -> list[dict]:
    """
    shots        — director shots, in order. Each may carry kf_id / scene / duration_sec.
    shot_beats   — {shot_index: [beat dicts]} from the pause detector on that shot's VO.
                   A shot with no detected beats (or no VO) falls back to one beat of
                   its director duration.
    Returns ordered render units:
      {
        order, shot_index, scene, kf_id,
        still_id,          # base kf_id for the shot's first clip; a unique
                           # "<kf_id>_b<beat>c<sub>" id for continuation stills
        beat_index,        # which beat within the shot
        sub_index,         # which clip within the beat
        is_continuation,   # True → generate a fresh continuation still of the same shot
        target_duration,   # how long this clip plays (all units sum to the full VO)
      }
    """
    plan: list[dict] = []
    order = 0
    for si, shot in enumerate(shots):
        kf_id = shot.get("kf_id", f"KF{si + 1}")
        scene = shot.get("scene", si + 1)
        beats = shot_beats.get(si)
        if not beats:
            dur = float(shot.get("duration_sec") or default_shot_seconds)
            beats = [{"start": 0.0, "end": dur, "duration": dur}]

        units = plan_beat_clips(beats, max_clip_seconds=max_clip_seconds)
        for u in units:
            is_first = (u["beat_index"] == 0 and u["sub_index"] == 0)
            still_id = kf_id if is_first else f"{kf_id}_b{u['beat_index']}c{u['sub_index']}"
            plan.append({
                "order": order,
                "shot_index": si,
                "scene": scene,
                "kf_id": kf_id,
                "still_id": still_id,
                "beat_index": u["beat_index"],
                "sub_index": u["sub_index"],
                "is_continuation": not is_first,
                "target_duration": u["target_duration"],
            })
            order += 1
    return plan


def build_edit_decisions(render_plan: list[dict], clip_manifest: dict) -> dict:
    """Turn the render plan + generated clips into edit_decisions the composer uses.

    One cut per render unit, in order, HARD CUT (cut on the pause), trimmed to the
    unit's target_duration (capped at the clip's real length). No crossfade overlap,
    so total visual length == sum of targets == the VO length, and the full VO lays
    over the timeline with no positioning or truncation.
    """
    by_still = {c.get("still_id") or c.get("clip_id"): c for c in clip_manifest.get("clips", [])}
    cuts: list[dict] = []
    order = 0
    for unit in render_plan:
        clip = by_still.get(unit["still_id"])
        if not clip:
            continue
        clip_len = float(clip.get("duration_seconds", unit["target_duration"]))
        trim_out = round(min(float(unit["target_duration"]), clip_len), 3)
        if trim_out <= 0:
            continue
        cuts.append({
            "order": order,
            "clip_id": clip["clip_id"],
            "still_id": unit["still_id"],
            "trim_in": 0.0,
            "trim_out": trim_out,
            "transition": "hard_cut",
            "transition_duration_seconds": 0.0,
            "scene": unit.get("scene"),
        })
        order += 1
    total = round(sum(c["trim_out"] - c["trim_in"] for c in cuts), 3)
    return {
        "version": "1.0",
        "cuts": cuts,
        "total_duration_seconds": max(total, 1.0),
        "music_start_offset_seconds": 0.0,
        "abt_timing": {},
    }


def plan_summary(plan: list[dict]) -> dict:
    """Rollups for cost estimation / logging."""
    n_clips = len(plan)
    n_continuation = sum(1 for u in plan if u["is_continuation"])
    total_seconds = round(sum(u["target_duration"] for u in plan), 3)
    return {
        "clips": n_clips,
        "base_stills": n_clips - n_continuation,
        "continuation_stills": n_continuation,
        "total_seconds": total_seconds,
    }
