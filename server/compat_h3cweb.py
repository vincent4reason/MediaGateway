"""h3cweb compat layer: the old :8600 API face served from Gateway jobs.

Mounted by server/main.py. Route registration order matters: the include line
sits before main.py's own /info so the old-format /info wins.

Documented diffs vs the frozen h3cweb server:
- job ids are "video_xxxxxxxx" (Gateway) not "h3_xxxxxxxx"
- /info: the video worker unloads the engine after every job, so device/model/
  cache are null / placeholder unless a job is running right now
- output lives at MG_ASSETS/<job_id>/output.mp4; a requested output_path is
  resolved against BASE_DIR and echoed back as requested_output_path but no
  file is written there — fetch via /v1/videos/{id}/content or read
  output_path directly. /files serves historical files only.
- missing ref / bad size return clean 4xx (old server crashed with 500 or
  Flask-style tuples)
"""
from __future__ import annotations

import base64
import os
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import core
from .workers import video

BASE_DIR = os.environ.get("H3CWEB_COMPAT_BASE_DIR", "/Users/vincent/code/h3cweb")
OUT_DIR = os.environ.get("H3CWEB_COMPAT_OUT_DIR",
                         os.path.join(BASE_DIR, "projects/p001/output/shots"))
REFS_DIR = os.environ.get("H3CWEB_COMPAT_REFS_DIR",
                          os.path.join(BASE_DIR, "projects/p001/output/refs"))

router = APIRouter()


class Ref(BaseModel):
    kind: str  # image | video | audio | video_audio
    path: str
    audio_path: Optional[str] = None
    include_embedded_audio: bool = False


class JobRequest(BaseModel):
    prompt: str
    refs: List[Ref] = []
    width: int = 864
    height: int = 480
    seconds: Optional[float] = None
    frames: Optional[int] = None
    steps: int = 6
    denoise_reuse: int = 1
    dit_layers: int = 45
    core_reuse: int = 1
    token_reduction: bool = False
    seed: Optional[int] = None
    output_path: Optional[str] = None
    ssd_streaming: bool = False
    reference_image_size: Optional[int] = None


def _resolve(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def _product_path(job_id: str) -> str:
    return str(core.ASSET_ROOT / job_id / "output.mp4")


# old status vocabulary; gateway "cancelled" surfaces as "failed" for legacy callers
_STATUS = {"queued": "queued", "running": "running", "completed": "completed",
           "failed": "failed", "cancelled": "failed"}


@router.get("/info")
def info():
    eng = video._engine
    device = model = cache = None
    if eng is not None:
        try:
            device, model, cache = eng.device(), eng.model(), eng.cache_info()
        except Exception:  # engine mid-load/close; placeholders are fine
            pass
    return {
        "engine": "h3.c",
        "version": "0.1.0-dev",
        "device": device,
        "model": model or {"dir": video.MODEL_DIR, "loaded": eng is not None},
        "cache": cache,
        # gateway extras (old callers ignore unknown keys)
        "engines": {t: {"mem_gb": m.MEM_GB, "module": m.__name__}
                    for t, m in core.registry().items()},
        "budget_gb": core.BUDGET_GB,
        "asset_root": str(core.ASSET_ROOT),
    }


@router.post("/jobs")
def create_job(req: JobRequest):
    d = req.model_dump()
    requested = d.pop("output_path")
    refs = []
    for r in d["refs"]:
        p = _resolve(r["path"])
        if not os.path.isfile(p):
            raise HTTPException(400, f"ref file not found: {r['path']}")
        r["path"] = p
        refs.append(r)
    d["refs"] = refs
    if requested:
        d["requested_output_path"] = _resolve(requested)
    job = core.create_job("video", d)
    out = {"job_id": job["id"], "status": "queued",
           "output_path": _product_path(job["id"])}
    if requested:
        out["requested_output_path"] = d["requested_output_path"]
    return out


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return {
        "job_id": job["id"],
        "status": _STATUS.get(job["status"], job["status"]),
        "phase": job["phase"] or ("queued" if job["status"] == "queued" else ""),
        "progress": job["progress"],
        "output_path": job["output_path"] or _product_path(job["id"]),
        "error": job["error"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "requested_output_path": job["params"].get("requested_output_path"),
    }


@router.get("/files/{name}")
def get_file(name: str):
    """Historical mp4s only; new job output lives in MG_ASSETS/<job_id>."""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".mp4"):
        raise HTTPException(400, "invalid file name")
    path = os.path.join(OUT_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="video/mp4")


# --- OpenAI Sora-style shim (open-storyboard-canvas custom provider) ---

_SORA_STATUS = {"queued": "queued", "running": "in_progress",
                "completed": "completed", "failed": "failed", "cancelled": "failed"}

_DATA_URL = re.compile(r"^data:image/([a-z0-9.+-]+);base64,(.*)$", re.S)
_DATA_EXT = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}


def _save_data_url(text: str) -> Optional[str]:
    """Decode a data:image URL to a file h3 can read. Non-data values are skipped."""
    m = _DATA_URL.match(text.strip())
    if not m:
        return None
    ext = _DATA_EXT.get(m.group(1).lower(), ".png")
    os.makedirs(REFS_DIR, exist_ok=True)
    path = os.path.join(REFS_DIR, f"ref_{uuid.uuid4().hex[:8]}{ext}")
    with open(path, "wb") as f:
        f.write(base64.b64decode(m.group(2)))
    return path


@router.post("/v1/videos")
async def openai_create_video(request: Request):
    """Accept JSON (Sora API style) and multipart (canvas OpenAI preset)."""
    ct = request.headers.get("content-type", "")
    ref_paths: list[str] = []
    if ct.startswith("multipart/") or ct.startswith("application/x-www-form"):
        form = await request.form()
        raw = {k: form.get(k) for k in ("prompt", "size", "seconds")}
        up = form.get("input_reference")
        if up is not None and hasattr(up, "read"):
            ext = os.path.splitext(getattr(up, "filename", "") or "")[1] or ".png"
            os.makedirs(REFS_DIR, exist_ok=True)
            path = os.path.join(REFS_DIR, f"ref_{uuid.uuid4().hex[:8]}{ext}")
            with open(path, "wb") as f:
                f.write(await up.read())
            ref_paths.append(path)
    else:
        try:
            raw = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON or form")
        # canvas sends one of these names depending on provider hints
        imgs = raw.get("input_reference")
        if imgs is None:
            imgs = raw.get("reference_images")
        if imgs is None:
            imgs = raw.get("images")
        if isinstance(imgs, str):
            imgs = [imgs]
        offered = len(imgs) if isinstance(imgs, list) else 0
        for item in imgs or []:
            if isinstance(item, str):
                path = _save_data_url(item)
                if path:
                    ref_paths.append(path)
        if offered and not ref_paths:
            raise HTTPException(400, "reference images not data URLs")
    prompt = re.sub(r"\s*\[IMAGE_\d+\]", "", str(raw.get("prompt") or "")).strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    # unsupported reference media: fail loudly, never silently drop
    for key in ("videos", "reference_videos", "video_url",
                "audios", "reference_audios", "audio_url"):
        if raw.get(key):
            raise HTTPException(400, f"{key} references not supported (images only)")
    size = str(raw.get("size") or "")
    seconds_raw = raw.get("seconds")
    try:
        seconds = float(seconds_raw) if seconds_raw else None
    except (TypeError, ValueError):
        seconds = None

    width, height = 864, 480
    if size:
        # canvas sends typographic ×, e.g. "512×288"
        size = size.strip().replace("×", "x").replace("X", "x")
        try:
            w, h = size.split("x", 1)
            width, height = int(w), int(h)
        except ValueError:
            raise HTTPException(400, "size must be WxH")
        # h3.c requires multiples of 32; canvas presets include 720x1280 etc.
        width -= width % 32
        height -= height % 32
    if width < 32 or height < 32:
        raise HTTPException(400, "resolution too small")
    if width * height > 768 * 1344:
        raise HTTPException(400, "resolution exceeds h3 768*1344 pixel limit")
    resp = create_job(JobRequest(
        prompt=prompt, width=width, height=height, seconds=seconds,
        refs=[Ref(kind="image", path=p) for p in ref_paths],
        # keep reference conditioning at native res (up to 2048px), not
        # stretched down to the render canvas
        reference_image_size=1 if ref_paths else 0))
    return {"id": resp["job_id"], "task_id": resp["job_id"], "status": "queued"}


@router.get("/v1/videos/{job_id}")
def openai_video_status(job_id: str, request: Request):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "video not found")
    status = _SORA_STATUS.get(job["status"], "in_progress")
    out = {
        "id": job_id,
        "status": status,
        "progress": job["progress"],
        "error": job["error"],
    }
    # 影策/newapi 协议在 succeeded 时从这里取结果地址；本地回环可达
    if status == "completed":
        out["url"] = str(request.base_url).rstrip("/") + f"/v1/videos/{job_id}/content"
    return out


@router.get("/v1/videos/{job_id}/content")
def openai_video_content(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "video not found")
    op = job["output_path"]
    if job["status"] != "completed" or not op or not os.path.isfile(op):
        raise HTTPException(409, "video not ready")
    return FileResponse(op, media_type="video/mp4")
