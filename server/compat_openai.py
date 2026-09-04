"""OpenAI-compatible image & audio faces for 影策 (openai-image / openai-audio).

影策 adapters (backend/internal/protocol/builtin.go):
  openai-image  POST /v1/images/generations  -> {data:[{b64_json}]}   (sync)
  openai-audio  POST /v1/audio/speech        -> JSON with url/data field
We run the local workers through the normal scheduler and wait synchronously —
both are short tasks (image ~70s, TTS ~10s) and the caller is local.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import core

router = APIRouter()

_WAIT_TIMEOUT = 900


def _wait_job(job_id: str, timeout: float = _WAIT_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = core.get_job(job_id)
        if job is None:
            raise HTTPException(500, "job vanished")
        if job["status"] in ("completed", "failed", "cancelled"):
            if job["status"] != "completed":
                raise HTTPException(500, job.get("error") or f"job {job['status']}")
            return job
        time.sleep(1)
    raise HTTPException(504, f"job did not finish in {timeout}s")


class ImageGenIn(BaseModel):
    model: Optional[str] = None
    prompt: str
    size: Optional[str] = None
    n: int = 1
    seed: Optional[int] = None


def _parse_size(size: Optional[str]) -> tuple[int, int]:
    width, height = 512, 512
    if size:
        try:
            w, h = size.lower().replace("×", "x").split("x", 1)
            width, height = int(w), int(h)
        except ValueError:
            raise HTTPException(400, "size must be WxH")
        width -= width % 32
        height -= height % 32
        if width < 32 or height < 32:
            raise HTTPException(400, "resolution too small")
    return width, height


def _gen_images(prompt: str, n: int, seed: Optional[int], extra: dict) -> list[dict]:
    data = []
    for i in range(n):
        params = {"prompt": prompt, **extra}
        if seed is not None:
            params["seed"] = seed + i
        job = _wait_job(core.create_job("image", params)["id"])
        with open(job["output_path"], "rb") as f:
            data.append({"b64_json": base64.b64encode(f.read()).decode()})
    return data


@router.post("/v1/images/generations")
def images_generations(req: ImageGenIn):
    n = max(1, min(int(req.n or 1), 4))
    width, height = _parse_size(req.size)
    return {"created": int(time.time()),
            "data": _gen_images(req.prompt, n, req.seed, {"width": width, "height": height})}


_MAGIC_EXTS = ((b"\x89PNG", "png"), (b"\xff\xd8", "jpg"), (b"P5", "ppm"), (b"P6", "ppm"))


def _save_ref(idx: int, data: bytes, tmpdir: str) -> str:
    """iris 只读 8-bit 非隔行 PNG/JPEG/PPM；其余（webp/heic/16bit/隔行...）转成 PNG。"""
    for magic, ext in _MAGIC_EXTS:
        if data.startswith(magic):
            if ext == "png" and (len(data) < 29 or data[24] != 8 or data[28] != 0):
                break  # IHDR: bit depth / interlace unsupported by iris -> normalize
            p = Path(tmpdir) / f"ref_{idx}.{ext}"
            p.write_bytes(data)
            return str(p)
    src = Path(tmpdir) / f"ref_{idx}.raw"
    src.write_bytes(data)
    dst = Path(tmpdir) / f"ref_{idx}.png"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(src), "-pix_fmt", "rgb24", "-y", str(dst)],
            capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is None or proc.returncode != 0 or not dst.is_file():
        raise HTTPException(400, f"reference image {idx}: unsupported format")
    return str(dst)


@router.post("/v1/images/edits")
async def images_edits(
    prompt: str = Form(...),
    size: Optional[str] = Form(None),
    n: int = Form(1),
    seed: Optional[int] = Form(None),
    image: list[UploadFile] = File([]),
    mask: Optional[UploadFile] = File(None),
):
    """影策带参考图生图（openai-images 协议的 edit_source 路径）。

    iris 以 in-context conditioning 叠加参考图（-i 可重复，至多 16 张）；mask 不支持。
    """
    if not image:
        raise HTTPException(400, "at least one input image is required")
    if len(image) > 16:
        raise HTTPException(400, "at most 16 reference images (iris MAX_INPUT_IMAGES)")
    for up in image:
        fsize = getattr(up.file, "size", None)
        if fsize is not None and fsize > 20 * 1024 * 1024:
            raise HTTPException(413, f"reference image too large (>20MB): {up.filename}")
    if mask is not None:
        raise HTTPException(400, "mask is not supported by the local image model")
    n = max(1, min(int(n or 1), 4))
    width, height = _parse_size(size)
    tmpdir = tempfile.mkdtemp(prefix="mg_edits_")
    try:
        refs = [_save_ref(i, await up.read(), tmpdir) for i, up in enumerate(image)]
        data = _gen_images(prompt, n, seed, {"width": width, "height": height, "input": refs})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"created": int(time.time()), "data": data}


class SpeechIn(BaseModel):
    model: Optional[str] = None
    input: str
    voice: Optional[str] = None
    speed: float = 1.0


def _known_voice(model: Optional[str]) -> Optional[str]:
    """影策渠道模型把 voiceId 放在 model 字段；命中 voices.json 才采纳。"""
    if not model:
        return None
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "vendor", "cosyvoice", "voices.json"), encoding="utf-8") as f:
            voices = json.load(f)
        return model if model in voices else None
    except (OSError, ValueError):
        return None


@router.post("/v1/audio/speech")
def audio_speech(req: SpeechIn, request: Request):
    if not req.input.strip():
        raise HTTPException(400, "input is required")
    voice = req.voice or _known_voice(req.model)
    params = {"text": req.input, "speed": req.speed}
    if voice:
        params["voice"] = voice  # else voice worker uses its default voice
    job = core.create_job("voice", params)
    job = _wait_job(job["id"], timeout=300)
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/v1/audio/jobs/{job['id']}/content"}


@router.get("/v1/audio/jobs/{job_id}/content")
def audio_content(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "audio not found")
    op = job["output_path"]
    if job["status"] != "completed" or not op or not os.path.isfile(op):
        raise HTTPException(409, "audio not ready")
    return FileResponse(op, media_type="audio/wav")
