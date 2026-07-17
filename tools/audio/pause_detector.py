"""
Pause detector — finds the natural pauses (silence gaps) in a voiceover file and
returns the speech "beats" between them.

Used by the Cinematic pipeline's pause-driven edit: the VO is generated first, its
pauses are detected here, and each speech beat becomes a visual beat (keyframe →
clip[s]) with the cut landing on the pause. This makes the visual rhythm follow the
narration's natural cadence instead of arbitrary per-shot durations.

Implementation: ffmpeg's `silencedetect` audio filter emits `silence_start` and
`silence_end` markers on stderr; we invert those intervals against the clip's total
duration to get the speech segments.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path


def _audio_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def detect_silences(
    audio_path: str,
    noise_db: float = -30.0,
    min_silence: float = 0.25,
) -> list[tuple[float, float]]:
    """Return a list of (silence_start, silence_end) intervals in seconds.

    noise_db     — anything quieter than this (dBFS) counts as silence.
    min_silence  — ignore silences shorter than this (avoids cutting on tiny gaps).
    """
    if not Path(audio_path).exists():
        return []
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", audio_path,
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
    )
    log = r.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    # Pair them up in order; a trailing silence may have a start with no end.
    silences: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else _audio_duration(audio_path)
        if e > s:
            silences.append((round(s, 3), round(e, 3)))
    return silences


def detect_beats(
    audio_path: str,
    noise_db: float = -30.0,
    min_silence: float = 0.25,
    min_beat: float = 0.6,
) -> dict:
    """Segment an audio file into speech beats separated by pauses.

    Returns:
      {
        "total_duration": float,           # full audio length
        "beats": [ {"start","end","duration"} , ... ],   # speech spans
        "pauses": [ {"start","end","duration"} , ... ],  # silence spans between beats
      }

    Beats shorter than `min_beat` are merged into the previous beat so a stray
    micro-gap (e.g. mid-word) doesn't create a jarring one-frame cut.
    """
    total = _audio_duration(audio_path)
    if total <= 0:
        return {"total_duration": 0.0, "beats": [], "pauses": []}

    silences = detect_silences(audio_path, noise_db=noise_db, min_silence=min_silence)

    # Build speech segments = the gaps BETWEEN silences across [0, total].
    raw_beats: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:
            raw_beats.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total:
        raw_beats.append((cursor, total))

    # No detectable silence at all → the whole thing is one beat.
    if not raw_beats:
        raw_beats = [(0.0, total)]

    # Merge beats shorter than min_beat into the previous one.
    merged: list[list[float]] = []
    for start, end in raw_beats:
        if merged and (end - start) < min_beat:
            merged[-1][1] = end
        else:
            merged.append([start, end])

    beats = [
        {"start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3)}
        for s, e in merged
    ]
    pauses = [
        {"start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3)}
        for s, e in silences
    ]
    return {"total_duration": round(total, 3), "beats": beats, "pauses": pauses}


def beats_to_segments(beats: list[dict], total_duration: float) -> list[dict]:
    """Turn speech beats into contiguous VISUAL segments that tile the whole audio.

    Each segment holds one beat's visual from the moment that beat's speech starts
    until the next beat's speech starts — i.e. it absorbs the trailing pause. So the
    cut to the next visual lands exactly on the next speech onset (right after a
    pause), and the segments sum to `total_duration` (visual length == audio length).
    """
    if total_duration <= 0:
        return []
    if not beats:
        return [{"start": 0.0, "end": round(total_duration, 3), "duration": round(total_duration, 3)}]
    segs: list[dict] = []
    for i, b in enumerate(beats):
        start = 0.0 if i == 0 else float(b["start"])
        end = float(beats[i + 1]["start"]) if i + 1 < len(beats) else float(total_duration)
        if end <= start:
            continue
        segs.append({"start": round(start, 3), "end": round(end, 3), "duration": round(end - start, 3)})
    return segs


def plan_beat_clips(beats: list[dict], max_clip_seconds: float = 8.0) -> list[dict]:
    """Split each speech beat into one or more clip units so a beat longer than a
    single model clip (e.g. Veo3's 8s cap) is covered by multiple chained clips.

    Each unit:
      beat_index    — which beat it belongs to
      sub_index     — 0-based position within the beat
      n_subclips    — how many clips this beat needs
      target_duration — how long this clip should play (sums to the beat duration)
      beat_start/beat_end — the beat's span in the VO timeline
    Units are returned in play order (beat 0's clips, then beat 1's, …).
    """
    units: list[dict] = []
    for bi, beat in enumerate(beats):
        dur = float(beat.get("duration", 0.0))
        if dur <= 0:
            continue
        n = max(1, math.ceil(round(dur / max_clip_seconds, 6)))
        remaining = dur
        for si in range(n):
            chunk = min(max_clip_seconds, remaining)
            units.append({
                "beat_index": bi,
                "sub_index": si,
                "n_subclips": n,
                "target_duration": round(chunk, 3),
                "beat_start": beat.get("start"),
                "beat_end": beat.get("end"),
            })
            remaining = round(remaining - chunk, 6)
    return units
