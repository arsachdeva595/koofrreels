"""
Google Vertex AI image client — Gemini image models (e.g. "gemini-2.5-flash-image",
the model also known as *Nano Banana*). Used as a Cinematic keyframe provider so stills
can be generated directly on Vertex — same model as fal's Nano Banana, no fal markup,
on infra you already have configured for Veo.

Reference images are passed as inline data for identity/character conditioning.

Auth: OAuth2 Bearer via google-auth (ADC or the service-account JSON configured in
Settings), exactly like the Veo client.

NOTE: built to the standard Vertex `:generateContent` shape. Exact model ID / region
availability can vary — both are configurable in Settings.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

_ENDPOINT_TMPL = (
    "https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:generateContent"
)
_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _encode(image_path: str) -> tuple[str, str]:
    """(base64, mime) for a reference image; convert exotic formats to JPEG."""
    suffix = Path(image_path).suffix.lower().lstrip(".")
    if suffix in _MIME:
        return base64.b64encode(Path(image_path).read_bytes()).decode(), _MIME[suffix]
    import io
    from PIL import Image as _PImage
    img = _PImage.open(image_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


class VertexImageClient(BaseTool):
    """Gemini image generation via Vertex AI REST (OAuth2)."""

    name = "vertex_image_client"
    version = "0.1.0"
    tier = ToolTier.AI
    capability = "text_to_image"
    provider = "google-vertex-ai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    def execute(self, params: dict) -> ToolResult:
        if params.get("operation", "image_generate") == "image_generate":
            return self._image_generate(params)
        return ToolResult(success=False, error=f"Unknown Vertex image operation: {params.get('operation')!r}")

    def _get_access_token(self) -> tuple[str | None, str | None]:
        try:
            import google.auth
            import google.auth.transport.requests
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(google.auth.transport.requests.Request())
            return credentials.token, None
        except Exception as exc:
            return None, (
                f"Google auth failed: {exc}. Run `gcloud auth application-default login` "
                "or set google_credentials_path in Settings → API Keys → Google Cloud."
            )

    def _image_generate(self, params: dict) -> ToolResult:
        prompt = params.get("prompt", "")
        image_paths = params.get("image_paths") or []
        model = params.get("model") or "gemini-2.5-flash-image"
        dest_path = params["dest_path"]
        project = params.get("vertex_project_id", "")
        location = params.get("vertex_location", "us-central1")
        if not project:
            return ToolResult(success=False, error="vertex_project_id not provided")

        token, err = self._get_access_token()
        if err:
            return ToolResult(success=False, error=err)

        parts: list[dict] = [{"text": prompt}]
        for p in image_paths:
            if Path(p).exists():
                b64, mime = _encode(p)
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})

        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        url = _ENDPOINT_TMPL.format(location=location, project=project, model=model)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(url, headers=headers, json=body)
                if not resp.is_success:
                    return ToolResult(success=False, error=f"Vertex image HTTP {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        # Find the first inline image in the response candidates.
        try:
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(dest_path).write_bytes(base64.b64decode(inline["data"]))
                        return ToolResult(success=True, data={"local_path": dest_path})
        except Exception as exc:
            return ToolResult(success=False, error=f"Could not parse Vertex image response: {exc}")
        return ToolResult(success=False, error=f"No image in Vertex response: {str(data)[:400]}")
