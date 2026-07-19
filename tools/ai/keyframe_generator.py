"""
Keyframe generator for the Cinematic reel type.

Wraps Nano Banana (Gemini image) via fal_client to turn each shot's image_prompt
(+ the one global style_lock + the reference image) into a 9:16 still. The stills
become frame 0 of the downstream image→video clips (M3), so their character/wardrobe
consistency is the whole ballgame (caveat C1).

Character lock is configurable:
  "sheet" — generate ONE character sheet first, then condition every still on
            [reference, character_sheet]. Default; most robust against drift.
  "chain" — feed the prior generated still into the next: [reference, prev_still].
  "none"  — condition only on the original reference each time.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from backend import settings_manager
from tools.ai.fal_client import FalClient
from tools.ai.realism_prompts import POSITIVE_REALISM_TAG, SILENT_CHARACTER_TAG

# Shot-type → plain-English framing so the image model honours the cinematography.
_SHOT_TYPE_PHRASE = {
    "ELS": "extreme long establishing shot, subject small in a wide environment",
    "LS": "long shot, full body within the setting",
    "MLS": "medium-long shot, knees up",
    "MS": "medium shot, waist up",
    "MCU": "medium close-up, chest up",
    "CU": "close-up, head and shoulders",
    "ECU": "extreme close-up detail",
    "Low": "low-angle power shot looking up at the subject",
    "High": "high-angle shot looking down at the subject",
    "Insert": "insert detail shot of a single object or texture",
}


class KeyframeGenerator:
    def __init__(self, provider: str | None = None, model: str | None = None) -> None:
        self.provider = (provider or settings_manager.get("cinematic_keyframe_provider", "fal") or "fal").lower()
        if self.provider == "google":
            self.model = model or settings_manager.get("cinematic_keyframe_google_model") or "gemini-2.5-flash-image"
        elif self.provider == "wavespeed":
            self.model = model or settings_manager.get("cinematic_keyframe_wavespeed_model") or "wavespeed-ai/flux-dev"
        else:
            self.model = model or settings_manager.get("cinematic_keyframe_endpoint") or "fal-ai/nano-banana/edit"
        self.fal = FalClient()

    # ── Provider-routed image generation ────────────────────────────────────────

    def _generate_image(self, prompt: str, reference_paths: list[str], dest_path: str):
        """Generate one still via the configured keyframe provider. Reference images
        (paths on disk) condition the result for character lock. fal / Google Vertex
        (Gemini image = 'Nano Banana') / WaveSpeed."""
        refs = [p for p in reference_paths if p and Path(p).exists()]
        if self.provider == "google":
            from tools.ai.vertex_image_client import VertexImageClient
            return VertexImageClient().execute({
                "operation": "image_generate", "prompt": prompt,
                "image_paths": refs, "model": self.model, "dest_path": dest_path,
                "vertex_project_id": settings_manager.get("vertex_project_id"),
                "vertex_location": settings_manager.get("vertex_location", "us-central1"),
                "aspect_ratio": "9:16",
            })
        if self.provider == "wavespeed":
            from tools.ai.wavespeed_client import WaveSpeedClient
            return WaveSpeedClient().execute({
                "operation": "image_generate", "model_slug": self.model, "prompt": prompt,
                "image_urls": [FalClient.encode_image(p) for p in refs], "dest_path": dest_path,
            })
        # fal (default)
        return self.fal.execute({
            "operation": "image_generate", "endpoint": self.model, "prompt": prompt,
            "image_urls": [FalClient.encode_image(p) for p in refs],
            "aspect_ratio": "9:16", "dest_path": dest_path,
        })

    # ── Prompt construction ─────────────────────────────────────────────────────

    def _framing(self, shot: dict) -> str:
        phrase = _SHOT_TYPE_PHRASE.get(shot.get("shot_type", "MS"), "medium shot")
        lens = shot.get("lens_mm")
        dof = shot.get("dof", "shallow")
        bits = [phrase]
        if lens:
            bits.append(f"{int(lens)}mm lens")
        if dof:
            bits.append(f"{dof} depth of field")
        return ", ".join(bits)

    def _still_prompt(self, shot: dict, style_lock: str) -> str:
        # LEAD with a hard identity lock — the model must render the SAME person from the
        # reference, not a look-alike (the previous "fresh composition first" phrasing let
        # weaker image models invent a new character). Then describe the new shot; only
        # pose/framing/lighting/background vary shot to shot.
        return (
            "Generate a photo of the SAME PERSON shown in the reference image(s). Keep their "
            "identity EXACTLY: identical face and facial features, same hairstyle, same skin tone, "
            "and the same outfit — identical prints, colours and accessories. It must be "
            "unmistakably the same individual, never a look-alike or a different person. "
            f"Place that exact person in a NEW shot — {self._framing(shot)}: {shot.get('image_prompt', '').strip()} "
            "Vertical 9:16 cinematic film still, photorealistic. Only the pose, camera framing, "
            "lighting and background change from shot to shot; the person and their wardrobe stay identical. "
            + ("Also include the PRODUCT shown in the product reference image — match its exact shape, "
               "colour and details; the person is holding / using / presenting it naturally. "
               if shot.get("feature_product") else "")
            + f"Overall look (affects grain/colour/lighting ONLY): {style_lock}. "
            f"{POSITIVE_REALISM_TAG}. "
            f"{SILENT_CHARACTER_TAG}. "
            "IMPORTANT: render NO text, words, letters, captions, watermarks, brand names or logos "
            "anywhere in the image — film-stock names like 'Kodak' describe the look, they must never "
            "appear as visible text."
        )

    # ── Single generations ──────────────────────────────────────────────────────

    def generate_character_sheet(self, reference_path: str, style_lock: str, dest_path: str):
        prompt = (
            "Character reference sheet of the SAME person shown in the reference image: "
            "neutral standing pose, front view, even soft studio light, plain neutral background, "
            "the full outfit and all accessories clearly visible, photorealistic. "
            "Keep an identical face, hairstyle and wardrobe to the reference. "
            f"STYLE (look only): {style_lock}. "
            "Render NO text, words, letters, watermarks, brand names or logos in the image."
        )
        return self._generate_image(prompt, [reference_path], dest_path)

    def generate_one(self, shot: dict, style_lock: str, reference_paths: list[str], dest_path: str):
        return self._generate_image(self._still_prompt(shot, style_lock), reference_paths, dest_path)

    # ── Sequence orchestration ──────────────────────────────────────────────────

    def _refs_for(self, strategy: str, ref_path: str, char_sheet_path: str | None, prev_path: str | None) -> list[str]:
        if strategy == "sheet":
            return [ref_path] + ([char_sheet_path] if char_sheet_path else [])
        if strategy == "chain":
            return [ref_path, prev_path] if (prev_path and prev_path != ref_path) else [ref_path]
        return [ref_path]

    def generate_sequence(
        self,
        shots: list[dict],
        style_lock: str,
        reference_path: str,
        keyframes_dir: str,
        strategy: str = "sheet",
        progress_cb: Callable[[int, int, str, bool], None] | None = None,
        product_image_path: str | None = None,
    ) -> dict:
        """Generate one still per shot. Returns {character_sheet, entries}.
        On shots flagged feature_product, the product image is added as an extra
        reference so the still shows the character with the product."""
        kf_dir = Path(keyframes_dir)
        kf_dir.mkdir(parents=True, exist_ok=True)
        _prod = product_image_path if (product_image_path and Path(product_image_path).exists()) else None

        def _shot_refs(base_refs: list, shot: dict) -> list:
            if _prod and shot.get("feature_product"):
                return base_refs + [_prod]
            return base_refs

        # Character sheet first (sheet strategy).
        char_sheet_path = None
        if strategy == "sheet":
            cs_dest = str(kf_dir / "character_sheet.png")
            cs = self.generate_character_sheet(reference_path, style_lock, cs_dest)
            if cs.success:
                char_sheet_path = cs_dest

        def _entry(i: int, shot: dict, res) -> dict:
            kf_id = shot.get("kf_id", f"KF{i + 1}")
            return {
                "kf_id": kf_id,
                "scene": i + 1,
                "filename": f"{kf_id}.png",
                "local_path": str(kf_dir / f"{kf_id}.png"),
                "image_prompt": shot.get("image_prompt", ""),
                "success": bool(res and res.success),
                "error": (res.error if res and not res.success else None),
            }

        entries: dict[int, dict] = {}
        done = 0

        if strategy == "chain":
            # Sequential — each still conditions on the previous one.
            prev_path = reference_path
            for i, shot in enumerate(shots):
                kf_id = shot.get("kf_id", f"KF{i + 1}")
                dest = str(kf_dir / f"{kf_id}.png")
                refs = _shot_refs(self._refs_for("chain", reference_path, None, prev_path), shot)
                res = self.generate_one(shot, style_lock, refs, dest)
                entries[i] = _entry(i, shot, res)
                if res.success:
                    prev_path = dest
                done += 1
                if progress_cb:
                    progress_cb(done, len(shots), kf_id, res.success)
        else:
            # sheet / none — independent stills, generate in parallel.
            def _gen(i_shot):
                i, shot = i_shot
                kf_id = shot.get("kf_id", f"KF{i + 1}")
                dest = str(kf_dir / f"{kf_id}.png")
                refs = _shot_refs(self._refs_for(strategy, reference_path, char_sheet_path, None), shot)
                return i, shot, self.generate_one(shot, style_lock, refs, dest)

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(_gen, (i, s)) for i, s in enumerate(shots)]
                for fut in as_completed(futures):
                    i, shot, res = fut.result()
                    entries[i] = _entry(i, shot, res)
                    done += 1
                    if progress_cb:
                        progress_cb(done, len(shots), entries[i]["kf_id"], res.success)

        return {
            "character_sheet": char_sheet_path,
            "entries": [entries[i] for i in sorted(entries)],
        }

    def generate_end_frame(
        self,
        shot: dict,
        style_lock: str,
        reference_path: str,
        char_sheet_path: str | None,
        dest_path: str,
    ):
        """Generate the END keyframe for a needs_end_frame shot (M3 first-last interpolation).
        Conditioned on the same references as the start still for character lock."""
        end_shot = {**shot, "image_prompt": shot.get("end_image_prompt") or shot.get("image_prompt", "")}
        cs = char_sheet_path if (char_sheet_path and Path(char_sheet_path).exists()) else None
        refs = [reference_path] + ([cs] if cs else [])
        return self.generate_one(end_shot, style_lock, refs, dest_path)

    def regenerate(
        self,
        shot: dict,
        style_lock: str,
        reference_path: str,
        char_sheet_path: str | None,
        dest_path: str,
        strategy: str = "sheet",
        product_image_path: str | None = None,
    ):
        """Regenerate a single still (used by the per-shot reshoot endpoint, C6).
        On a feature_product shot, the product image is re-added as a reference so the
        reshot still keeps the product."""
        cs = char_sheet_path if (strategy == "sheet" and char_sheet_path and Path(char_sheet_path).exists()) else None
        refs = self._refs_for("sheet" if cs else "none", reference_path, cs, None)
        if product_image_path and Path(product_image_path).exists() and shot.get("feature_product"):
            refs = refs + [product_image_path]
        return self.generate_one(shot, style_lock, refs, dest_path)
