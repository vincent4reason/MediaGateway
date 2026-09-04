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
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
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


@router.post("/v1/images/generations")
def images_generations(req: ImageGenIn):
    width, height = 512, 512
    if req.size:
        try:
            w, h = req.size.lower().replace("×", "x").split("x", 1)
            width, height = int(w), int(h)
        except ValueError:
            raise HTTPException(400, "size must be WxH")
        width -= width % 32
        height -= height % 32
        if width < 32 or height < 32:
            raise HTTPException(400, "resolution too small")
    n = max(1, min(int(req.n or 1), 4))
    data = []
    for i in range(n):
        params = {"prompt": req.prompt, "width": width, "height": height}
        if req.seed is not None:
            params["seed"] = req.seed + i
        job = _wait_job(core.create_job("image", params)["id"])
        with open(job["output_path"], "rb") as f:
            data.append({"b64_json": base64.b64encode(f.read()).decode()})
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
