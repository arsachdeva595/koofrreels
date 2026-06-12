from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

import asyncio
import subprocess

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import MUSIC_LIBRARY_PATH, UPLOADS_DIR
from backend.job_manager import JobStatus, job_manager
from backend.pipeline_runner import run_pipeline
from backend.meme_runner import run_meme_pipeline
from backend.audio_runner import run_audio_pipeline
from backend.stock_runner import run_stock_pipeline
from backend.studio_runner import run_studio_pipeline
from backend.ai_runner import run_ai_pipeline
from backend.studio_constants import MODEL_LIBRARY
from backend import settings_manager, queue_manager
from tools.koofr.koofr_browser import KoofrBrowser
from tools.publer.publer_client import PublerClient

# Apply saved settings to env on startup
settings_manager.load()

app = FastAPI(title="KoofrReels", version="1.0.0")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ── Static frontend ────────────────────────────────────────────────────────────
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    index = frontend_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>KoofrReels</h1><p>Frontend not found.</p>")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    page = frontend_dir / "settings.html"
    if page.exists():
        return HTMLResponse(page.read_text())
    raise HTTPException(status_code=404, detail="Settings page not found")


# ── Koofr endpoints ────────────────────────────────────────────────────────────

@app.get("/koofr/folders")
async def list_folders():
    browser = KoofrBrowser()
    result = browser.execute({"operation": "list_folders"})
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


@app.get("/koofr/clips")
async def list_clips(path: str = "/"):
    browser = KoofrBrowser()
    result = browser.execute({"operation": "list_clips", "path": path})
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


# ── Music library ──────────────────────────────────────────────────────────────

@app.get("/music/tracks")
async def list_music():
    tracks = []
    if MUSIC_LIBRARY_PATH.exists():
        for ext in ["*.mp3", "*.m4a", "*.wav", "*.ogg"]:
            for f in MUSIC_LIBRARY_PATH.glob(ext):
                tracks.append({"filename": f.name, "size_bytes": f.stat().st_size})
    return {"tracks": tracks}


# ── Settings ───────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    s = settings_manager.load()
    # Mask secrets before sending to frontend
    masked = dict(s)
    for key in ["koofr_app_password", "anthropic_api_key", "publer_api_key", "pexels_api_key", "elevenlabs_api_key", "fal_api_key", "wavespeed_api_key"]:
        if masked.get(key):
            masked[key] = "••••••••"
    return masked


class SettingsUpdate(BaseModel):
    koofr_email: str | None = None
    koofr_app_password: str | None = None
    anthropic_api_key: str | None = None
    publer_api_key: str | None = None
    publer_workspace_id: str | None = None
    pexels_api_key: str | None = None
    koofr_default_folder: str | None = None
    brand_voice: str | None = None
    publer_enabled: bool | None = None
    publer_default_account_ids: list[str] | None = None
    publer_auto_schedule: bool | None = None
    publer_schedule_time: str | None = None
    pexels_gap_fill_enabled: bool | None = None
    queue_auto_run: bool | None = None
    queue_run_interval_hours: int | None = None
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    meme_master_prompt: str | None = None
    local_output_dir: str | None = None
    fal_api_key: str | None = None
    wavespeed_api_key: str | None = None
    vertex_project_id: str | None = None
    vertex_location: str | None = None
    google_credentials_path: str | None = None


@app.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    # Don't overwrite secrets with mask placeholder
    updates = {k: v for k, v in updates.items() if v != "••••••••"}
    saved = settings_manager.save(updates)
    _secret_keys = {"koofr_app_password", "anthropic_api_key", "publer_api_key", "pexels_api_key", "elevenlabs_api_key", "fal_api_key", "wavespeed_api_key"}
    return {"ok": True, "settings": {k: ("••••••••" if k in _secret_keys and v else v) for k, v in saved.items()}}


@app.get("/api/settings/brand-voice")
async def get_brand_voice():
    return {"brand_voice": settings_manager.get_brand_voice()}


# ── Publer endpoints ───────────────────────────────────────────────────────────

@app.get("/api/publer/accounts")
async def publer_accounts():
    client = PublerClient()
    result = await asyncio.to_thread(client.execute, {"operation": "list_accounts"})
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


class PublerPushRequest(BaseModel):
    job_id: str
    account_ids: list[str]
    caption: str = ""
    schedule_at: str | None = None


@app.post("/api/publer/push")
async def publer_push(req: PublerPushRequest):
    job = job_manager.get(req.job_id)
    if not job or job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Job not completed")

    output_path = job.result.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="Output file not found")

    client = PublerClient()

    # Upload media — run in thread to avoid blocking the async event loop
    upload = await asyncio.to_thread(
        client.execute, {"operation": "upload_media", "file_path": output_path}
    )
    if not upload.success:
        raise HTTPException(status_code=502, detail=f"Media upload failed: {upload.error}")

    media_id = upload.data["media_id"]

    # Schedule post
    schedule_params: dict[str, Any] = {
        "operation": "schedule_post",
        "account_ids": req.account_ids,
        "caption": req.caption,
        "media_ids": [media_id],
    }
    if req.schedule_at:
        schedule_params["schedule_at"] = req.schedule_at

    post = await asyncio.to_thread(client.execute, schedule_params)
    if not post.success:
        raise HTTPException(status_code=502, detail=f"Scheduling failed: {post.error}")

    return {"ok": True, "post_id": post.data.get("post_id"), "media_id": media_id}


# ── Native folder picker ───────────────────────────────────────────────────────

@app.post("/api/pick-folder")
async def pick_folder():
    """Open a native macOS folder-picker dialog and return the chosen path."""
    import platform
    if platform.system() != "Darwin":
        raise HTTPException(status_code=501, detail="Native folder picker is only available on macOS. Set the output folder path manually in Settings.")

    def _run_picker():
        return subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Select output folder for KoofrReels")'],
            capture_output=True, text=True, timeout=120,
        )
    r = await asyncio.to_thread(_run_picker)
    if r.returncode != 0:
        raise HTTPException(status_code=400, detail="No folder selected")
    return {"path": r.stdout.strip()}


# ── Reel generation ────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    mode: str = Field(default="random", pattern="^(random|describe|browse|folder)$")
    target_duration_seconds: float = Field(default=30, ge=15, le=90)
    koofr_folder: str | None = None
    clip_ids: list[str] | None = None
    prompt: str | None = None
    music_file: str | None = None
    include_text: bool = True
    include_voiceover: bool = True
    text_hints: str | None = None


@app.post("/reel/generate")
async def generate_reel(req: GenerateRequest):
    # Apply default folder from settings if not specified
    params = req.model_dump()
    if not params.get("koofr_folder") and params.get("mode") == "folder":
        params["koofr_folder"] = settings_manager.get_koofr_folder()

    job = job_manager.create()

    def run():
        try:
            run_pipeline(job, params)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


@app.get("/reel/status/{job_id}")
async def get_status(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


class ApprovalRequest(BaseModel):
    text_plan: dict | None = None       # legacy
    scenes: list[dict] | None = None    # new per-scene edits


@app.post("/reel/{job_id}/approve")
async def approve_step(job_id: str, body: ApprovalRequest):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise HTTPException(status_code=400, detail=f"Job is not awaiting approval (status: {job.status})")
    job.submit_approval(body.model_dump(exclude_none=True))
    return {"ok": True}


@app.get("/reel/download/{job_id}")
async def download_reel(job_id: str, request: Request):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, not completed")

    output_path = job.result.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="Output file not found")

    # On the server, use X-Accel-Redirect so nginx serves the file directly
    # (much faster than streaming through uvicorn). Falls back to FileResponse locally.
    accel_base = "/opt/koofrreels/"
    if Path(output_path).is_absolute() and output_path.startswith(accel_base):
        relative = output_path[len(accel_base):]
        return JSONResponse(
            content={},
            headers={
                "X-Accel-Redirect": f"/internal-files/{relative}",
                "Content-Type": "video/mp4",
                "Content-Disposition": 'attachment; filename="reel.mp4"',
            },
        )

    return FileResponse(path=output_path, media_type="video/mp4", filename="reel.mp4")


@app.get("/jobs")
async def list_jobs():
    return {"jobs": job_manager.all()}


# ── Queue endpoints ────────────────────────────────────────────────────────────

@app.get("/api/queue")
async def get_queue():
    return {"queue": queue_manager.list_queue()}


class QueueIdeaRequest(BaseModel):
    mode: str = "random"
    target_duration_seconds: float = 30
    koofr_folder: str | None = None
    prompt: str | None = None
    music_file: str | None = None
    include_text: bool = True
    text_hints: str | None = None
    notes: str = ""
    publer_push: bool = False
    publer_account_ids: list[str] = []
    publer_caption: str = ""
    publer_schedule_at: str | None = None


@app.post("/api/queue")
async def add_to_queue(req: QueueIdeaRequest):
    entry = queue_manager.add_idea(req.model_dump())
    return {"ok": True, "entry": entry}


@app.delete("/api/queue/{idea_id}")
async def remove_from_queue(idea_id: str):
    ok = queue_manager.delete_idea(idea_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return {"ok": True}


# ── Meme Clip endpoints ────────────────────────────────────────────────────────

class MemeGenerateRequest(BaseModel):
    prompt: str = ""
    clip_id: str | None = None
    koofr_folder: str | None = None
    target_duration_seconds: float = Field(default=8.0, ge=3, le=30)
    music_file: str | None = None
    include_voiceover: bool = True


@app.post("/meme/generate")
async def generate_meme(req: MemeGenerateRequest):
    params = req.model_dump()
    if not params.get("koofr_folder"):
        params["koofr_folder"] = settings_manager.get_koofr_folder()

    job = job_manager.create()

    def run():
        try:
            run_meme_pipeline(job, params)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


# ── Audio Clip endpoints ───────────────────────────────────────────────────────

class AudioGenerateRequest(BaseModel):
    prompt: str = ""
    audio_path: str | None = None    # absolute path (from upload)
    audio_file: str | None = None    # filename in music library
    koofr_folder: str | None = None
    mode: str = "describe"
    include_voiceover: bool = True


@app.post("/audio/generate")
async def generate_audio(req: AudioGenerateRequest):
    params = req.model_dump()

    # Resolve audio to an absolute path
    if params.get("audio_file"):
        resolved = MUSIC_LIBRARY_PATH / params["audio_file"]
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="Audio file not found in library")
        params["audio_path"] = str(resolved)

    if not params.get("audio_path"):
        raise HTTPException(status_code=400, detail="audio_path or audio_file is required")

    if not params.get("koofr_folder"):
        params["koofr_folder"] = settings_manager.get_koofr_folder()

    job = job_manager.create()

    def run():
        try:
            run_audio_pipeline(job, params)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/audio/upload")
async def upload_audio(file: UploadFile = File(...)):
    audio_dir = UPLOADS_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "audio.mp3").suffix or ".mp3"
    safe_name = f"{uuid.uuid4().hex[:8]}{suffix}"
    dest = audio_dir / safe_name

    content = await file.read()
    dest.write_bytes(content)

    return {"path": str(dest), "filename": file.filename or safe_name}


# ── Stock (Pexels) endpoints ───────────────────────────────────────────────────

class StockGenerateRequest(BaseModel):
    target_duration_seconds: float = Field(default=30, ge=15, le=90)
    prompt: str | None = None
    music_file: str | None = None
    include_text: bool = True
    include_voiceover: bool = True
    use_brand_guidelines: bool = True
    text_hints: str | None = None
    skip_storyboard: bool = False                 # use prompt as raw scene script, skip Claude


@app.post("/stock/generate")
async def generate_stock(req: StockGenerateRequest):
    params = req.model_dump()
    job = job_manager.create()

    def run():
        try:
            run_stock_pipeline(job, params)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


# ── AI Reels (Veo3) endpoints ─────────────────────────────────────────────────

class AIGenerateRequest(BaseModel):
    target_duration_seconds: float = Field(default=30, ge=15, le=90)
    prompt: str | None = None
    music_file: str | None = None
    include_text: bool = True
    include_voiceover: bool = True
    use_brand_guidelines: bool = True
    text_hints: str | None = None
    reel_type: str = "story"                      # "story" | "character" | "none" | "product"
    character_image_path: str | None = None
    product_image_path: str | None = None         # product mode — anchors hero scenes
    keyframe_paths: list[str] | None = None
    skip_storyboard: bool = False                 # use prompt as raw scene script, skip Claude
    skip_edit_planner: bool = False               # use clips as-is, skip cut planning
    framework: str = "abt"                        # script framework: abt|pas|aida|hvc|hpsp


@app.post("/ai/generate")
async def generate_ai_reel(req: AIGenerateRequest):
    params = req.model_dump()
    job = job_manager.create()

    def run():
        try:
            run_ai_pipeline(job, params)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


@app.post("/ai/{job_id}/approve")
async def approve_ai(job_id: str, body: ApprovalRequest):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise HTTPException(status_code=400, detail=f"Job is not awaiting approval (status: {job.status})")
    job.submit_approval(body.model_dump(exclude_none=True))
    return {"ok": True}


@app.post("/api/ai/upload-image")
async def upload_ai_image(file: UploadFile = File(...)):
    img_dir = UPLOADS_DIR / "ai"
    img_dir.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    original_suffix = Path(file.filename or "image.jpg").suffix.lower()
    safe_stem = uuid.uuid4().hex[:8]

    _PASSTHROUGH = {".jpg", ".jpeg", ".png", ".webp"}
    if original_suffix not in _PASSTHROUGH:
        try:
            import io
            import pillow_heif
            pillow_heif.register_heif_opener()
            from PIL import Image as _PImage
            img = _PImage.open(io.BytesIO(content)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            content = buf.getvalue()
            original_suffix = ".jpg"
        except Exception:
            pass

    safe_name = f"{safe_stem}{original_suffix}"
    dest = img_dir / safe_name
    dest.write_bytes(content)
    return {"path": str(dest), "filename": safe_name}


# ── Studio endpoints ───────────────────────────────────────────────────────────

class StudioGenerateRequest(BaseModel):
    product_image_path: str
    model_image_path: str | None = None
    library_model_id: str | None = None
    product_name: str = "tote bag"
    style_prompt: str | None = None
    music_file: str | None = None
    shot_controls: dict | None = None


@app.post("/studio/generate")
async def generate_studio(req: StudioGenerateRequest):
    if not Path(req.product_image_path).exists():
        raise HTTPException(status_code=400, detail="product_image_path does not exist on disk")

    params = req.model_dump()
    job = job_manager.create()

    def run():
        try:
            run_studio_pipeline(job, params)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/studio/upload-image")
async def upload_studio_image(file: UploadFile = File(...)):
    img_dir = UPLOADS_DIR / "studio"
    img_dir.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    original_suffix = Path(file.filename or "image.jpg").suffix.lower()
    safe_stem = uuid.uuid4().hex[:8]

    # Convert HEIC/HEIF and any non-JPEG/PNG format to JPEG
    _PASSTHROUGH = {".jpg", ".jpeg", ".png", ".webp"}
    if original_suffix not in _PASSTHROUGH:
        try:
            import io
            import pillow_heif
            pillow_heif.register_heif_opener()
            from PIL import Image as _PImage
            img = _PImage.open(io.BytesIO(content)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            content = buf.getvalue()
            original_suffix = ".jpg"
        except Exception:
            pass  # fall through and save as-is if conversion fails

    safe_name = f"{safe_stem}{original_suffix}"
    dest = img_dir / safe_name
    dest.write_bytes(content)
    return {"path": str(dest), "filename": safe_name}


@app.get("/api/studio/models")
async def studio_models():
    return {"models": MODEL_LIBRARY}


@app.post("/studio/{job_id}/approve")
async def approve_studio(job_id: str, body: ApprovalRequest):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise HTTPException(status_code=400, detail=f"Job is not awaiting approval (status: {job.status})")
    job.submit_approval(body.model_dump(exclude_none=True))
    return {"ok": True}


@app.post("/api/queue/{idea_id}/run")
async def run_queue_item(idea_id: str):
    items = queue_manager.list_queue()
    item = next((i for i in items if i["id"] == idea_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    job = job_manager.create()
    queue_manager.update_idea(idea_id, {"status": "running", "job_id": job.job_id})

    def run():
        try:
            run_pipeline(job, item["brief"])
            queue_manager.update_idea(idea_id, {"status": "completed", "job_id": job.job_id})

            # Auto-push to Publer if configured
            if item.get("publer_push") and job.status == JobStatus.COMPLETED:
                output_path = job.result.get("output_path")
                if output_path and Path(output_path).exists():
                    client = PublerClient()
                    upload = client.execute({"operation": "upload_media", "file_path": output_path})
                    if upload.success:
                        client.execute({
                            "operation": "schedule_post",
                            "account_ids": item.get("publer_account_ids", []),
                            "caption": item.get("publer_caption", ""),
                            "media_ids": [upload.data["media_id"]],
                            "schedule_at": item.get("publer_schedule_at"),
                        })
        except Exception:
            queue_manager.update_idea(idea_id, {"status": "failed"})

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job.job_id, "status": "running"}
