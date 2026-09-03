"""AI Media Gateway — unified job API.

Env: MG_DB, MG_ASSETS, MG_BUDGET_GB, MG_PORT (default 8600).
Run: .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8600
"""
from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import core

app = FastAPI(title="AI Media Gateway")


class JobIn(BaseModel):
    type: str
    params: dict = {}
    priority: int = 0


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
