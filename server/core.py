"""Gateway core: worker contract, auto-discovery registry, SQLite jobs, scheduler.

Worker contract — each module in server/workers/<name>.py defines:
    TYPE: str                      # job type, e.g. "video"
    MEM_GB: float                  # estimated peak RSS in GB (drives budget scheduling)
    def run(params: dict, job_dir: Path, progress, cancel) -> dict
        params   : request params (validated by the worker itself)
        job_dir  : Path — write all outputs here
        progress : f(ratio: float, phase: str)
        cancel   : f() -> bool — check periodically; raise any Exception to abort
        returns  : {"output_path": str, ...meta} recorded on the job

Cancellation semantics: cancel() is cooperative. A job that ignores it may
still finish "completed" after cancel was requested — the final DB status is
always authoritative. Cancel does NOT retry and does NOT kill subprocesses.

Engine-resident memory: workers may hold loaded models between jobs
(e.g. video keep_loaded, voice tts_server). _admit_next adds those resident
amounts to the budget check via the worker modules' module-level state.
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from . import workers as workers_pkg

BASE = Path(__file__).resolve().parent.parent
_LLM_MODULE = "server.llm"  # optional face; absence => scheduler behaviour unchanged
DB_PATH = Path(os.environ.get("MG_DB", BASE / "data" / "gateway.db"))
ASSET_ROOT = Path(os.environ.get("MG_ASSETS", BASE / "assets"))
BUDGET_GB = float(os.environ.get("MG_BUDGET_GB", "40"))

CancelledError = Exception  # workers may raise any exception; message goes to job.error


# --- worker registry (auto-discovery, zero shared-file edits when adding workers) ---

_REGISTRY: dict[str, dict] = {}


def registry() -> dict[str, dict]:
    if _REGISTRY:
        return _REGISTRY
    for mod in pkgutil.iter_modules(workers_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        m = importlib.import_module(f"{workers_pkg.__name__}.{mod.name}")
        if hasattr(m, "TYPE") and hasattr(m, "run"):
            _REGISTRY[m.TYPE] = m
    return _REGISTRY


# --- SQLite job store ---

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|completed|failed|cancelled
    priority INTEGER NOT NULL DEFAULT 0,
    params TEXT NOT NULL DEFAULT '{}',
    output_path TEXT,
    meta TEXT,
    progress REAL NOT NULL DEFAULT 0,
    phase TEXT,
    error TEXT,
    created_at REAL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority DESC, created_at);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_db_init_lock = threading.Lock()


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _db_init_lock:  # first request and scheduler thread can race here
            if _conn is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.executescript(_SCHEMA)
                # crash recovery: running jobs died with the old process —
                # their threads are gone; leaving them 'running' would poison
                # the memory budget forever. Queued jobs are still valid.
                cur = conn.execute(
                    "UPDATE jobs SET status='failed', "
                    "error='interrupted by gateway restart', finished_at=? "
                    "WHERE status='running'", (time.time(),))
                if cur.rowcount:
                    print(f"[core] recovered {cur.rowcount} orphaned running job(s)",
                          flush=True)
                conn.commit()
                _conn = conn
    return _conn


def _job_row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d["params"])
    d["meta"] = json.loads(d["meta"]) if d["meta"] else None
    return d


def create_job(jtype: str, params: dict, priority: int = 0) -> dict:
    job_id = f"{jtype}_{uuid.uuid4().hex[:8]}"
    now = time.time()
    with _lock:
        db().execute(
            "INSERT INTO jobs (id, type, priority, params, created_at) VALUES (?,?,?,?,?)",
            (job_id, jtype, priority, json.dumps(params), now))
        db().commit()
    return get_job(job_id)


def get_job(job_id: str) -> dict | None:
    r = db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_row(r) if r else None


def _update(job_id: str, **fields):
    if not fields:
        return
    sets, vals = zip(*[(k, json.dumps(v) if k in ("meta", "params") else v)
                       for k, v in fields.items()])
    with _lock:
        db().execute(f"UPDATE jobs SET {','.join(k + '=?' for k in sets)} WHERE id=?",
                     (*vals, job_id))
        db().commit()


def cancel_job(job_id: str) -> bool:
    job = get_job(job_id)
    if not job:
        return False
    if job["status"] == "running":
        _CANCELLED.add(job_id)  # cooperative; worker checks via cancel()
        return True
    if job["status"] == "queued":
        with _lock:
            cur = db().execute(
                "UPDATE jobs SET status='cancelled', finished_at=? "
                "WHERE id=? AND status='queued'",  # lost race => it's running now
                (time.time(), job_id))
            db().commit()
        if cur.rowcount:
            return True
        # lost the race — it started running while we updated; re-check so a
        # job that actually finished in between is not marked cancellable
        job = get_job(job_id)
        if job and job["status"] == "running":
            _CANCELLED.add(job_id)
            return True
        return False
    return False


_CANCELLED: set[str] = set()


# --- scheduler: single picker thread, memory-budget gate, one thread per job ---

_pick_lock = threading.Lock()  # serialize admit decisions


def _resident_gb(reg: dict) -> float:
    """Memory held by engines/processes between jobs (invisible to the
    per-job accounting): video keep_loaded engine, voice tts_server."""
    gb = 0.0
    video_mod = reg.get("video")
    if video_mod is not None and getattr(video_mod, "_engine", None) is not None:
        gb += video_mod.MEM_GB
    voice_mod = reg.get("voice")
    proc = getattr(voice_mod, "_proc", None) if voice_mod else None
    if voice_mod is not None and proc is not None and proc.poll() is None:
        gb += voice_mod.MEM_GB
    llm_mod = sys.modules.get(_LLM_MODULE)  # absent => no LLM face loaded
    if llm_mod is not None and llm_mod.resident():
        gb += llm_mod.MEM_GB
    return gb


def _admit_next() -> tuple[str, dict] | None:
    reg = registry()
    running_gb = sum(
        (reg[r["type"]].MEM_GB if r["type"] in reg else BUDGET_GB)  # unknown => assume worst
        for r in db().execute("SELECT type FROM jobs WHERE status='running'"))
    running_gb += _resident_gb(reg)
    q = db().execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC, created_at")
    for row in q:
        worker = reg.get(row["type"])
        if worker is None:
            _update(row["id"], status="failed", error=f"unknown type: {row['type']}",
                    finished_at=time.time())
            continue
        # GPU mutual exclusion: video/shot can't share 48GB with the resident
        # LLM. Ask it to unload, skip this round; the next poll re-checks until
        # the LLM process is gone. Absent llm module => behaviour unchanged.
        if row["type"] in ("video", "shot"):
            llm_mod = sys.modules.get(_LLM_MODULE)
            if llm_mod is not None:
                if getattr(llm_mod, "busy", lambda: False)():
                    break  # chat in flight / mid-spawn — wait it out, no thrash
                if llm_mod.resident():
                    getattr(llm_mod, "request_unload", lambda: None)()
                    break
        if running_gb + worker.MEM_GB <= BUDGET_GB:
            return worker, _job_row(row)
        # budget-blocked: stop at first job that doesn't fit (FIFO head blocking
        # keeps priority honest; jobs behind it may fit but wait their turn)
        break
    return None


def _run_job(worker, job: dict):
    jid = job["id"]
    job_dir = ASSET_ROOT / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    _update(jid, status="running", started_at=time.time())
    try:
        result = worker.run(
            job["params"], job_dir,
            progress=lambda ratio, phase="": _update(jid, progress=ratio, phase=phase),
            cancel=lambda: jid in _CANCELLED,
        )
        if jid in _CANCELLED:
            raise CancelledError("cancelled")
        _update(jid, status="completed", progress=1.0,
                output_path=result.get("output_path"),
                meta={k: v for k, v in result.items() if k != "output_path"},
                finished_at=time.time())
    except Exception as e:  # noqa: BLE001
        status = "cancelled" if jid in _CANCELLED else "failed"
        _update(jid, status=status, error=str(e), finished_at=time.time())
    finally:
        _CANCELLED.discard(jid)
        _update(jid)  # no-op keepalive; statuses above are authoritative


def scheduler_loop(poll_s: float = 0.5):
    while True:
        with _pick_lock:
            nxt = _admit_next()
        if nxt:
            worker, job = nxt
            _update(job["id"], status="running", started_at=time.time())
            threading.Thread(target=_run_job, args=(worker, job), daemon=True).start()
        time.sleep(poll_s)
