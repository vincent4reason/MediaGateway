"""Tests for server/core.py recovery + cancel semantics — no GPU.

Run: .venv/bin/python tests/test_core.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import core  # noqa: E402


def _fresh_db():
    """Point core at a new temp DB and reset the cached connection."""
    d = tempfile.mkdtemp(prefix="core_test_")
    core.DB_PATH = Path(d) / "gateway.db"
    with core._db_init_lock:
        core._conn = None
    return core.db()


def test_startup_recovery_marks_running_failed():
    db = _fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('x1', 'video', 'running', 0)")
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('x2', 'video', 'queued', 0)")
    db.commit()
    # simulate restart: drop cached conn, reopen (recovery runs on init)
    with core._db_init_lock:
        core._conn = None
    db2 = core.db()
    assert db2.execute("SELECT status FROM jobs WHERE id='x1'").fetchone()[0] == "failed"
    assert db2.execute("SELECT status FROM jobs WHERE id='x2'").fetchone()[0] == "queued"


def test_cancel_finished_job_returns_false():
    db = _fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('f1', 'video', 'completed', 0)")
    db.commit()
    core._CANCELLED.discard("f1")
    assert core.cancel_job("f1") is False
    assert "f1" not in core._CANCELLED  # no zombie entry for finished jobs


def test_budget_counts_resident_engines():
    class FakeVideo:
        MEM_GB = 35.0
        _engine = object()  # resident (keep_loaded)

    class FakeVoice:
        MEM_GB = 8.7

        class _P:
            @staticmethod
            def poll():
                return None  # alive

        _proc = _P()

    saved = dict(core._REGISTRY)
    try:
        core._REGISTRY.update({"video": FakeVideo, "voice": FakeVoice})
        gb = core._resident_gb(core._REGISTRY)
        assert abs(gb - 43.7) < 0.01, gb  # 35 (engine) + 8.7 (tts alive)
    finally:
        core._REGISTRY.clear()
        core._REGISTRY.update(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
