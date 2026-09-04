"""AI Media Gateway — unified job API.

Env: MG_DB, MG_ASSETS, MG_BUDGET_GB, MG_PORT (default 8600).
Run: .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8600
"""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import core

app = FastAPI(title="AI Media Gateway")

from .compat_h3cweb import router as compat_h3cweb_router  # noqa: E402 — h3cweb :8600 face
app.include_router(compat_h3cweb_router)  # registered before /info below so old format wins

from .compat_openai import router as compat_openai_router  # noqa: E402 — 影策 openai-image / openai-audio faces
app.include_router(compat_openai_router)

from .compat_chat import router as compat_chat_router  # noqa: E402 — 影策 openai-chat face → local qwen LLM
app.include_router(compat_chat_router)


class JobIn(BaseModel):
    type: str
    params: dict = {}
    priority: int = 0


class ShotIn(BaseModel):
    """Composite shot spec — each stage dict is optional but one is required."""
    image: Optional[dict] = None
    video: Optional[dict] = None
    voice: Optional[dict] = None
    music: Optional[dict] = None
    mix: Optional[dict] = None


@app.on_event("startup")
def _startup():
    core.registry()  # fail fast on broken worker modules
    threading.Thread(target=core.scheduler_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/info")
def info():
    return {
        "engines": {t: {"mem_gb": m.MEM_GB, "module": m.__name__}
                    for t, m in core.registry().items()},
        "budget_gb": core.BUDGET_GB,
        "asset_root": str(core.ASSET_ROOT),
    }


@app.post("/v1/jobs")
def create_job(req: JobIn):
    if req.type not in core.registry():
        raise HTTPException(400, f"unknown type: {req.type} (available: {sorted(core.registry())})")
    return core.create_job(req.type, req.params, req.priority)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not core.cancel_job(job_id):
        raise HTTPException(409, "job not cancellable (unknown/finished)")
    return core.get_job(job_id)


@app.post("/v1/shots/{shot_id}/render")
def render_shot(shot_id: str, req: ShotIn):
    params = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    params["shot_id"] = shot_id
    return core.create_job("shot", params)
