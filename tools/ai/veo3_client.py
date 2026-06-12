"""
Google Veo3 client — text-to-video via Vertex AI.

Uses the Vertex AI long-running operation pattern:
  POST  https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/
        publishers/google/models/veo-3.0-generate-preview:predictLongRunning  → {operation_name}
  GET   https://{location}-aiplatform.googleapis.com/v1/{operation_name}       → poll until done
  Decode base64 video bytes from the operation response and write to disk.

Auth: OAuth2 Bearer token via google-auth (ADC or service account JSON).
  - Recommended: run `gcloud auth application-default login` once.
  - Alternative: set google_credentials_path in Settings to a service account JSON file.

Vertex AI does NOT support plain API keys — OAuth2 is required.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx

from tools.ai.prompt_sanitizer import sanitize_prompt
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

# Default model — fast/cheap text- and first-frame-image-to-video.
VEO3_MODEL = "veo-3.1-lite-generate-001"
# Reference-capable model — supports referenceImages ("asset") so multiple subjects
# (e.g. a creator AND a product) can be preserved in the same shot. Used by UGC mode.
# NOTE: if your Vertex project has a different reference-capable Veo 3.1 model enabled,
# change this ID (e.g. "veo-3.1-generate-001").
VEO3_REFERENCE_MODEL = "veo-3.1-generate-001"

_ENDPOINT_TMPL = (
    "https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:{verb}"
)
OPERATIONS_BASE = "https://aiplatform.googleapis.com/v1"
POLL_INTERVAL = 10   # seconds between status checks
MAX_WAIT = 300       # 5 minutes per clip
VEO3_MAX_DURATION = 8


def _encode_asset_image(image_path: str) -> tuple[str, str]:
    """Return (base64_bytes, mime_type) for a Veo reference image.
    Reference images accept only image/jpeg and image/png — anything else
    (webp, heic, etc.) is converted to JPEG."""
    suffix = Path(image_path).suffix.lower().lstrip(".")
    if suffix in ("jpg", "jpeg"):
        return base64.b64encode(Path(image_path).read_bytes()).decode(), "image/jpeg"
    if suffix == "png":
        return base64.b64encode(Path(image_path).read_bytes()).decode(), "image/png"
    # Convert anything else to JPEG
    import io
    from PIL import Image as _PImage
    img = _PImage.open(image_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


class Veo3Client(BaseTool):
    """Google Veo3 text-to-video client via Vertex AI REST API (API key auth)."""

    name = "veo3_client"
    version = "0.1.0"
    tier = ToolTier.AI
    capability = "text_to_video"
    provider = "google-vertex-ai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    def execute(self, params: dict) -> ToolResult:
        operation = params.get("operation", "text_to_video")
        if operation == "text_to_video":
            return self._text_to_video(params)
        return ToolResult(success=False, error=f"Unknown Veo3 operation: {operation!r}")

    # ── Auth ───────────────────────────────────────────────────────────────────

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _get_access_token(self) -> tuple[str | None, str | None]:
        """Return (bearer_token, error) using ADC or service account JSON.

        Priority:
          1. GOOGLE_APPLICATION_CREDENTIALS env var (set by settings_manager when
             google_credentials_path is configured in Settings)
          2. gcloud Application Default Credentials
             (run: gcloud auth application-default login)
        """
        try:
            import google.auth
            import google.auth.transport.requests

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(google.auth.transport.requests.Request())
            return credentials.token, None
        except Exception as exc:
            return None, (
                f"Google auth failed: {exc}. "
                "Either run `gcloud auth application-default login` "
                "or set google_credentials_path in Settings → API Keys → Google Cloud."
            )

    def _bearer_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── API calls ──────────────────────────────────────────────────────────────

    def _submit_generation(
        self,
        project_id: str,
        location: str,
        prompt: str,
        token: str,
        image_path: str | None = None,
        negative_prompt: str = "",
        reference_image_paths: list[str] | None = None,
        model: str = VEO3_MODEL,
    ) -> tuple[str | None, str | None]:
        """POST to Veo3 predictLongRunning. Returns (operation_name, error)."""
        url = _ENDPOINT_TMPL.format(location=location, project=project_id, model=model, verb="predictLongRunning")
        instance: dict = {"prompt": prompt}
        if reference_image_paths:
            # Reference ("asset") images — preserve multiple subjects (e.g. creator +
            # product) in the same shot. Max 3 per Veo docs. JPEG/PNG only.
            ref_imgs = []
            for p in reference_image_paths[:3]:
                b64, mime = _encode_asset_image(p)
                ref_imgs.append({"image": {"bytesBase64Encoded": b64, "mimeType": mime}, "referenceType": "asset"})
            instance["referenceImages"] = ref_imgs
        elif image_path:
            suffix = Path(image_path).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix, "image/jpeg")
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(Path(image_path).read_bytes()).decode(),
                "mimeType": mime,
            }
        parameters: dict = {"sampleCount": 1}
        if negative_prompt:
            parameters["negativePrompt"] = negative_prompt
        payload = {
            "instances": [instance],
            "parameters": parameters,
        }
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(url, headers=self._bearer_headers(token), json=payload)
                if not resp.is_success:
                    return None, f"Veo3 submit HTTP {resp.status_code}: {resp.text[:600]}"
                data = resp.json()
                op_name = data.get("name")
                if not op_name:
                    return None, f"No operation name in Veo3 response: {data}"
                return op_name, None
        except Exception as exc:
            return None, str(exc)

    def _fetch_operation(
        self,
        operation_name: str,
        project_id: str,
        location: str,
        token: str,
        timeout: int = MAX_WAIT,
        interval: int = POLL_INTERVAL,
        model: str = VEO3_MODEL,
    ) -> tuple[dict | None, str | None]:
        """Poll via fetchPredictOperation until done=True or timeout.

        Per docs: POST .../<model>:fetchPredictOperation
        with body {"operationName": "<full operation name>"}
        """
        url = _ENDPOINT_TMPL.format(location=location, project=project_id, model=model, verb="fetchPredictOperation")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.post(
                        url,
                        headers=self._bearer_headers(token),
                        json={"operationName": operation_name},
                    )
                    if not r.is_success:
                        return None, f"Veo3 poll HTTP {r.status_code}: {r.text[:400]}"
                    data = r.json()
                    if "error" in data:
                        msg = data["error"].get("message", str(data["error"]))
                        return None, f"Veo3 operation error: {msg}"
                    if data.get("done"):
                        return data, None
            except Exception as exc:
                return None, str(exc)
        return None, f"Veo3 generation timed out after {timeout}s"

    def _save_video(self, operation_response: dict, dest_path: str) -> str | None:
        """Decode base64 video bytes from the operation response and write to dest_path.
        Returns error string or None on success.

        Response shape (no storageUri):
          response.videos[0].bytesBase64Encoded
        """
        try:
            videos = operation_response.get("response", {}).get("videos", [])
            for vid in videos:
                b64 = vid.get("bytesBase64Encoded")
                if b64:
                    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(dest_path).write_bytes(base64.b64decode(b64))
                    return None
            return f"No video bytes in Veo3 response: {str(operation_response)[:400]}"
        except Exception as exc:
            return str(exc)

    # ── Main operation ─────────────────────────────────────────────────────────

    def _text_to_video(self, params: dict) -> ToolResult:
        """
        Generate a video clip from a text prompt using Veo3.

        params:
          prompt              str        scene visual_description (+ clip_hint)
          dest_path           str        local path to save the .mp4
          vertex_project_id   str        GCP project ID
          vertex_location     str        Vertex AI region (e.g. "us-central1")
          image_path          str|None   optional reference/keyframe image path (first frame)
          negative_prompt     str        optional content to exclude (e.g. speech)
          reference_image_paths list|None optional asset images (e.g. creator + product)
                                          preserved together in the shot (UGC mode)
        """
        prompt = params.get("prompt", "")
        dest_path = params["dest_path"]
        project_id = params.get("vertex_project_id", "")
        location = params.get("vertex_location", "us-central1")
        image_path = params.get("image_path")
        negative_prompt = params.get("negative_prompt", "")
        reference_image_paths = params.get("reference_image_paths") or None
        # Reference images require the reference-capable model; otherwise use the default.
        model = params.get("model") or (VEO3_REFERENCE_MODEL if reference_image_paths else VEO3_MODEL)

        if not project_id:
            return ToolResult(success=False, error="vertex_project_id not provided")

        prompt, subs = sanitize_prompt(prompt)
        if subs:
            import logging
            logging.getLogger(__name__).info("Prompt sanitized: %s", "; ".join(subs))

        token, err = self._get_access_token()
        if err:
            return ToolResult(success=False, error=err)

        op_name, err = self._submit_generation(
            project_id, location, prompt, token, image_path, negative_prompt,
            reference_image_paths=reference_image_paths, model=model,
        )
        if err:
            return ToolResult(success=False, error=err)

        op_response, err = self._fetch_operation(op_name, project_id, location, token, model=model)
        if err:
            return ToolResult(success=False, error=f"{err} [op={op_name}]")

        err = self._save_video(op_response, dest_path)
        if err:
            return ToolResult(success=False, error=err)

        return ToolResult(success=True, data={"local_path": dest_path})
