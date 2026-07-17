"""
Cross-shot colour matcher for the Cinematic reel type (caveat C4).

Independently generated clips come back at slightly different exposure / white
balance even with one style_lock. This applies ONE consistent grade across all
clips before compose: it measures each clip's mean R/G/B from a sampled frame,
picks a per-channel target (the median across clips), and nudges every clip toward
it with a single ffmpeg colorchannelmixer gain. The goal is consistency between
shots, not a creative look.

Never fatal — if a clip can't be measured or graded, the original is kept.
"""
from __future__ import annotations

import statistics
import subprocess
from pathlib import Path


class ColorMatcher:
    # Clamp per-channel gain so one mis-measured clip can't blow out the grade.
    MIN_GAIN = 0.6
    MAX_GAIN = 1.6

    def _frame_means(self, clip_path: str) -> tuple[float, float, float] | None:
        """Mean R/G/B of a frame sampled ~1s in, via ffmpeg → PIL."""
        try:
            from PIL import Image
            tmp = Path(clip_path).with_suffix(".grade_probe.jpg")
            cmd = ["ffmpeg", "-nostdin", "-y", "-ss", "1", "-i", clip_path,
                   "-frames:v", "1", "-q:v", "3", str(tmp)]
            r = subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            if r.returncode != 0 or not tmp.exists():
                # Retry from the very start (clip may be shorter than 1s).
                cmd[cmd.index("-ss") + 1] = "0"
                subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            if not tmp.exists():
                return None
            img = Image.open(tmp).convert("RGB")
            # Downscale for a fast, stable mean.
            img.thumbnail((160, 160))
            px = list(img.getdata())
            n = len(px)
            means = (sum(p[0] for p in px) / n, sum(p[1] for p in px) / n, sum(p[2] for p in px) / n)
            tmp.unlink(missing_ok=True)
            return means
        except Exception:
            return None

    def _grade(self, clip_path: str, gains: tuple[float, float, float], dest: str) -> bool:
        vf = f"colorchannelmixer=rr={gains[0]:.4f}:gg={gains[1]:.4f}:bb={gains[2]:.4f}"
        base = ["ffmpeg", "-nostdin", "-y", "-i", clip_path, "-vf", vf, "-c:a", "copy"]
        hw = base + ["-c:v", "h264_videotoolbox", "-q:v", "55", dest]
        r = subprocess.run(hw, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and Path(dest).exists():
            return True
        sw = base + ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", dest]
        r = subprocess.run(sw, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        return r.returncode == 0 and Path(dest).exists()

    def match(self, clip_paths: list[str], output_dir: str) -> list[str]:
        """Return graded clip paths (same order). Falls back to originals on failure."""
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        means = [self._frame_means(p) for p in clip_paths]
        valid = [m for m in means if m]
        if len(valid) < 2:
            return clip_paths  # not enough signal to match against

        target = tuple(statistics.median(m[c] for m in valid) for c in range(3))

        graded: list[str] = []
        for path, m in zip(clip_paths, means):
            if not m:
                graded.append(path)
                continue
            gains = tuple(
                max(self.MIN_GAIN, min(self.MAX_GAIN, target[c] / max(m[c], 1.0)))
                for c in range(3)
            )
            # If already within ~3% on every channel, skip the re-encode.
            if all(abs(g - 1.0) < 0.03 for g in gains):
                graded.append(path)
                continue
            dest = str(out_dir / f"graded_{Path(path).name}")
            graded.append(dest if self._grade(path, gains, dest) else path)
        return graded
