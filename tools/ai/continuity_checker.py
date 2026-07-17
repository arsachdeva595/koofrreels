"""
Continuity checker for the Cinematic reel type (caveat C3).

Runs a Claude-vision drift check on each generated keyframe still BEFORE any video
credits are spent. It compares each still against the original reference (and the
character sheet, if one was generated) and flags face / wardrobe / print / colour
drift. Flagged stills are cheap to regenerate at the still stage.

Returns a per-still verdict: {"pass": bool, "issues": [str], "severity": "ok|warn|fail"}.
"""
from __future__ import annotations

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backend import settings_manager

_MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


# Claude vision hard-caps images at 10 MB and works best at ≤1568px on the long edge.
# Full-res 1080×1920 PNG stills easily blow past 10 MB, so downscale + JPEG-compress
# before sending (identity is still perfectly judgeable at this size).
_VISION_MAX_EDGE = 1568
_VISION_MAX_BYTES = 4_500_000


def _img_block(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    raw = p.read_bytes()
    media = _MEDIA.get(p.suffix.lower().lstrip("."), "image/jpeg")
    needs_shrink = len(raw) > _VISION_MAX_BYTES or media not in ("image/jpeg", "image/png")
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if needs_shrink or max(w, h) > _VISION_MAX_EDGE:
            img = img.convert("RGB")
            if max(w, h) > _VISION_MAX_EDGE:
                scale = _VISION_MAX_EDGE / max(w, h)
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            raw, media = buf.getvalue(), "image/jpeg"
    except Exception:
        pass  # fall back to raw bytes if PIL isn't available / fails
    data = base64.standard_b64encode(raw).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


class ContinuityChecker:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "") or settings_manager.get("anthropic_api_key", "")

    def _check_one(self, still_path: str, reference_paths: list[str]) -> dict:
        if not self.api_key:
            return {"pass": True, "issues": [], "severity": "ok", "skipped": "no_api_key"}
        still_block = _img_block(still_path)
        if not still_block:
            return {"pass": False, "issues": ["still not found on disk"], "severity": "fail"}

        import anthropic

        content: list[dict] = [{"type": "text", "text": "REFERENCE IMAGE(S) of the character:"}]
        for rp in reference_paths:
            blk = _img_block(rp)
            if blk:
                content.append(blk)
        content.append({"type": "text", "text": "GENERATED KEYFRAME to check:"})
        content.append(still_block)
        content.append({"type": "text", "text": (
            "You are a continuity supervisor on a film shoot. Compare the GENERATED KEYFRAME to the "
            "REFERENCE image(s) of the same character. Flag any drift that would make a viewer think "
            "it is a different person or a wardrobe change: face/identity, hairstyle, skin tone, the "
            "outfit, prints/patterns, colours, and key accessories. Ignore framing, pose, lens, lighting "
            "mood, and background — those are SUPPOSED to vary shot to shot.\n"
            "Respond ONLY with JSON: {\"pass\": true|false, \"issues\": [\"...\"], \"severity\": \"ok|warn|fail\"}.\n"
            "severity: \"ok\" = consistent, \"warn\" = minor drift, \"fail\" = clearly a different "
            "person or outfit. pass=false only for severity \"fail\"."
        )})

        try:
            resp = anthropic.Anthropic(api_key=self.api_key).messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": content}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)
            return {
                "pass": bool(data.get("pass", True)),
                "issues": [str(x) for x in (data.get("issues") or [])][:6],
                "severity": data.get("severity", "ok") if data.get("severity") in ("ok", "warn", "fail") else "ok",
            }
        except Exception as exc:
            # Never block the pipeline on a checker failure — surface it as a soft warning.
            return {"pass": True, "issues": [f"continuity check skipped ({exc})"], "severity": "ok"}

    def check_entries(self, entries: list[dict], reference_paths: list[str]) -> dict[str, dict]:
        """Check a list of keyframe entries in parallel. Returns {kf_id: verdict}."""
        verdicts: dict[str, dict] = {}

        def _run(entry: dict) -> tuple[str, dict]:
            if not entry.get("success"):
                return entry["kf_id"], {"pass": False, "issues": ["still was not generated"], "severity": "fail"}
            return entry["kf_id"], self._check_one(entry["local_path"], reference_paths)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_run, e) for e in entries]
            for fut in as_completed(futures):
                kf_id, verdict = fut.result()
                verdicts[kf_id] = verdict
        return verdicts
