"""
fal.ai API client — image-to-video, image-to-image, text-to-image, virtual try-on.

Uses the fal.ai async queue pattern:
  POST  https://queue.fal.run/{endpoint}           → {request_id}
  GET   …/requests/{request_id}/status             → poll until COMPLETED
  GET   …/requests/{request_id}                    → fetch result
  Download video/image from the result URL.
"""
from __future__ import annotations

import base64
import os
import time
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

FAL_QUEUE_BASE = "https://queue.fal.run"
POLL_INTERVAL = 5   # seconds between status checks
MAX_WAIT = 600      # 10 minutes per request


class FalClient(BaseTool):
    """Generic fal.ai client supporting multiple model endpoints."""

    name = "fal_client"
    version = "0.1.0"
    tier = ToolTier.AI
    capability = "ai_video_generation"
    provider = "fal.ai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    def execute(self, params: dict) -> ToolResult:
        operation = params.get("operation", "")
        if operation == "image_to_video":
            return self._image_to_video(params)
        if operation == "image_to_image":
            return self._image_to_image(params)
        if operation == "text_to_image":
            return self._text_to_image(params)
        if operation == "tryon":
            return self._tryon(params)
        if operation == "remove_background":
            return self._remove_background(params)
        return ToolResult(success=False, error=f"Unknown fal operation: {operation!r}")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _api_key(self) -> str:
        return os.getenv("FAL_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Key {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _submit(self, endpoint: str, payload: dict) -> tuple[str | None, str | None]:
        """Submit a job to the fal queue. Returns (request_id, error)."""
        api_key = self._api_key()
        if not api_key:
            return None, "FAL_API_KEY not set"
        url = f"{FAL_QUEUE_BASE}/{endpoint}"
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(url, headers=self._headers(), json=payload)
                if not resp.is_success:
                    return None, f"fal.ai HTTP {resp.status_code}: {resp.text[:500]}"
                return resp.json()["request_id"], None
        except Exception as exc:
            return None, str(exc)

    def _poll(self, endpoint: str, request_id: str) -> tuple[dict | None, str | None]:
        """Poll until COMPLETED or timeout. Returns (result_dict, error)."""
        status_url = f"{FAL_QUEUE_BASE}/{endpoint}/requests/{request_id}/status"
        result_url = f"{FAL_QUEUE_BASE}/{endpoint}/requests/{request_id}"
        deadline = time.time() + MAX_WAIT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.get(status_url, headers=self._headers())
                    r.raise_for_status()
                    data = r.json()
                    status = data.get("status", "")
                    if status == "COMPLETED":
                        res = client.get(result_url, headers=self._headers())
                        res.raise_for_status()
                        return res.json(), None
                    if status == "FAILED":
                        return None, f"fal.ai job FAILED: {data}"
            except Exception as exc:
                return None, str(exc)
        return None, "fal.ai polling timed out after 10 minutes"

    def _download(self, url: str, dest_path: str) -> str | None:
        """Stream-download a URL to dest_path. Returns error string or None."""
        try:
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            with httpx.Client(timeout=300, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(dest_path, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            fh.write(chunk)
            return None
        except Exception as exc:
            return str(exc)

    @staticmethod
    def encode_image(image_path: str) -> str:
        """Base64-encode a local image as a data URI."""
        suffix = Path(image_path).suffix.lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
        data = Path(image_path).read_bytes()
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    # ── Operations ─────────────────────────────────────────────────────────────

    def _image_to_video(self, params: dict) -> ToolResult:
        """
        Animate an image into a short video clip.

        params:
          endpoint    str   e.g. "fal-ai/kling-video/v2.1/pro/image-to-video"
          image_url   str   data URI or https URL
          prompt      str
          duration    int   seconds (default 5)
          dest_path   str   local path to save .mp4
        """
        endpoint = params["endpoint"]
        image_url = params["image_url"]
        prompt = params.get("prompt", "")
        duration = params.get("duration", 5)
        dest_path = params["dest_path"]

        payload: dict = {"image_url": image_url, "prompt": prompt}
        if duration:
            payload["duration"] = str(duration)  # Kling expects string enum "5" or "10"
        if params.get("aspect_ratio"):
            payload["aspect_ratio"] = params["aspect_ratio"]

        request_id, err = self._submit(endpoint, payload)
        if err:
            return ToolResult(success=False, error=err)

        result, err = self._poll(endpoint, request_id)
        if err:
            return ToolResult(success=False, error=err)

        video_url = (result or {}).get("video", {}).get("url") or (result or {}).get("url")
        if not video_url:
            return ToolResult(success=False, error=f"No video URL in fal result: {result}")

        err = self._download(video_url, dest_path)
        if err:
            return ToolResult(success=False, error=err)

        return ToolResult(success=True, data={"local_path": dest_path, "video_url": video_url})

    def _image_to_image(self, params: dict) -> ToolResult:
        """
        Edit/transform an image (FLUX Kontext, background swap, etc.).

        params:
          endpoint    str   e.g. "fal-ai/flux-pro/kontext"
          image_url   str   data URI or https URL
          prompt      str
          dest_path   str   local path to save result image (.jpg/.png)
        """
        endpoint = params["endpoint"]
        image_url = params["image_url"]
        prompt = params.get("prompt", "")
        dest_path = params["dest_path"]

        payload = {"image_url": image_url, "prompt": prompt}

        request_id, err = self._submit(endpoint, payload)
        if err:
            return ToolResult(success=False, error=err)

        result, err = self._poll(endpoint, request_id)
        if err:
            return ToolResult(success=False, error=err)

        images = (result or {}).get("images") or []
        img_url = images[0].get("url") if images else None
        if not img_url:
            return ToolResult(success=False, error=f"No image URL in fal result: {result}")

        err = self._download(img_url, dest_path)
        if err:
            return ToolResult(success=False, error=err)

        return ToolResult(success=True, data={"local_path": dest_path, "image_url": img_url})

    def _text_to_image(self, params: dict) -> ToolResult:
        """
        Generate an image from a text prompt (FLUX Pro, etc.).

        params:
          endpoint    str   e.g. "fal-ai/flux-pro"
          prompt      str
          dest_path   str   local path to save result image
        """
        endpoint = params["endpoint"]
        prompt = params.get("prompt", "")
        dest_path = params["dest_path"]

        payload = {"prompt": prompt}

        request_id, err = self._submit(endpoint, payload)
        if err:
            return ToolResult(success=False, error=err)

        result, err = self._poll(endpoint, request_id)
        if err:
            return ToolResult(success=False, error=err)

        images = (result or {}).get("images") or []
        img_url = images[0].get("url") if images else None
        if not img_url:
            return ToolResult(success=False, error=f"No image URL in fal result: {result}")

        err = self._download(img_url, dest_path)
        if err:
            return ToolResult(success=False, error=err)

        return ToolResult(success=True, data={"local_path": dest_path, "image_url": img_url})

    def _remove_background(self, params: dict) -> ToolResult:
        """
        Remove background from a product image using BiRefNet.

        params:
          image_url   str   data URI or https URL of the product image
          dest_path   str   local path to save result PNG (transparent background)
        """
        endpoint = "fal-ai/birefnet"
        image_url = params["image_url"]
        dest_path = params["dest_path"]

        request_id, err = self._submit(endpoint, {"image_url": image_url})
        if err:
            return ToolResult(success=False, error=err)

        result, err = self._poll(endpoint, request_id)
        if err:
            return ToolResult(success=False, error=err)

        img_url = (result or {}).get("image", {}).get("url")
        if not img_url:
            # some versions return images array
            images = (result or {}).get("images") or []
            img_url = images[0].get("url") if images else None
        if not img_url:
            return ToolResult(success=False, error=f"No image URL in birefnet result: {result}")

        err = self._download(img_url, dest_path)
        if err:
            return ToolResult(success=False, error=err)

        return ToolResult(success=True, data={"local_path": dest_path, "image_url": img_url})

    def _tryon(self, params: dict) -> ToolResult:
        """
        Virtual try-on via FASHN: places garment on a model photo.

        params:
          product_image_url   str   data URI of garment/product
          model_image_url     str   data URI of person photo
          category            str   FASHN category (default "bags")
          dest_path           str   local path to save result image
        """
        endpoint = "fal-ai/fashn/tryon"
        product_url = params["product_image_url"]
        model_url = params["model_image_url"]
        category = params.get("category", "bags")
        dest_path = params["dest_path"]

        payload = {
            "garment_photo_url": product_url,
            "model_photo_url": model_url,
            "category": category,
        }

        request_id, err = self._submit(endpoint, payload)
        if err:
            return ToolResult(success=False, error=err)

        result, err = self._poll(endpoint, request_id)
        if err:
            return ToolResult(success=False, error=err)

        images = (result or {}).get("images") or []
        img_url = images[0].get("url") if images else None
        if not img_url:
            return ToolResult(success=False, error=f"No try-on image in fal result: {result}")

        err = self._download(img_url, dest_path)
        if err:
            return ToolResult(success=False, error=err)

        return ToolResult(success=True, data={"local_path": dest_path, "image_url": img_url})
