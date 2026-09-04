#!/usr/bin/env python3
"""LLM face + mutex tests — stub upstream via stdlib http.server, no GPU.

Run: .venv/bin/python tests/test_compat_chat.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# isolated DB + a QWEN port that will never hold a real server (unload sweeps
# it harmlessly) — both must be set before importing server.core / server.llm
os.environ["MG_DB"] = str(Path(tempfile.mkdtemp(prefix="chat_test_")) / "gateway.db")
os.environ["QWEN_PORT"] = "8899"
os.environ["QWEN_IDLE_EXIT_S"] = "9999"  # keep the watchdog out of the tests

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server import core, llm  # noqa: E402
from server.compat_chat import router  # noqa: E402

_app = FastAPI()
_app.include_router(router)
client = TestClient(_app)


# --- stub upstream (stands in for the local mtplx server) ---

class Stub(BaseHTTPRequestHandler):
    hits = 0
    last_body = None

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": True})

    def do_POST(self):
        Stub.hits += 1
        Stub.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(200, {"id": "cmpl-1", "object": "chat.completion",
                         "choices": [{"index": 0, "message": {
                             "role": "assistant", "content": "雨夜东京"}, "finish_reason": "stop"}]})


def with_stub(fn):
    Stub.hits = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    old = llm.BASE_URL
    llm.BASE_URL = url
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        llm.BASE_URL = old
        srv.shutdown()


# --- stub llm module injected into sys.modules for core mutex tests ---

def make_llm_stub(resident=True, mem=16.0):
    stub = types.ModuleType("server.llm")
    stub.MEM_GB = mem
    stub.calls = []
    stub.resident = lambda: resident
    stub.request_unload = lambda: stub.calls.append("unload")
    return stub


def fresh_db():
    d = tempfile.mkdtemp(prefix="chat_test_db_")
    core.DB_PATH = Path(d) / "gateway.db"
    core._db_initialized = False
    core._tls.conn = None
    return core.db()


def queue_job(jtype):
    db = fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               f"VALUES ('j_{jtype}', '{jtype}', 'queued', 0)")
    db.commit()


def test_mutex_blocks_video_while_llm_resident():
    queue_job("video")
    stub = make_llm_stub(resident=True)
    sys.modules["server.llm"] = stub
    try:
        assert core._admit_next() is None          # video skipped this round
        assert stub.calls == ["unload"]            # ...and unload requested
    finally:
        del sys.modules["server.llm"]


def test_mutex_releases_once_llm_gone():
    queue_job("video")
    sys.modules["server.llm"] = make_llm_stub(resident=False)
    try:
        nxt = core._admit_next()
        assert nxt is not None and nxt[1]["type"] == "video"
    finally:
        del sys.modules["server.llm"]


def test_llm_memory_counts_against_budget():
    queue_job("noop")  # noop MEM_GB=0.1, budget 40
    sys.modules["server.llm"] = make_llm_stub(resident=True, mem=39.95)
    try:
        # 39.95GB resident + 0.1 noop > 40GB budget => blocked
        assert core._admit_next() is None
    finally:
        del sys.modules["server.llm"]
    assert core._admit_next() is not None  # stub gone => admits again


def test_no_llm_module_behaviour_unchanged():
    queue_job("noop")
    sys.modules.pop("server.llm", None)
    assert core._admit_next() is not None


def test_chat_forwards_to_upstream():
    def go():
        r = client.post("/v1/chat/completions", json={
            "model": "qwen3.8-27b",
            "messages": [{"role": "user", "content": "写一句雨夜东京的短台词"}]})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "雨夜东京"
        assert Stub.last_body["model"] == "qwen3.8-27b"
        assert Stub.last_body["messages"][0]["content"].startswith("写一句")
    with_stub(go)


def test_chat_503_while_video_running():
    db = fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('v_run', 'video', 'running', 0)")
    db.commit()

    def go():
        r = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 503, r.status_code
        assert "视频生成中" in r.json()["error"]["message"]
        assert r.json()["error"]["retryable"] is True
        assert Stub.hits == 0  # never reached upstream
    with_stub(go)


def test_chat_rejects_stream():
    def go():
        r = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 400
    with_stub(go)


def test_request_unload_defers_while_busy():
    llm._unload_pending = False
    with llm.busy_guard():
        llm.request_unload()
        assert llm._unload_pending is True  # deferred, port untouched
    llm._unload_pending = False  # reset for later tests


def test_unload_with_no_server_is_noop_true():
    llm._proc = None
    assert llm.unload() is True
    assert llm.resident() is False


if __name__ == "__main__":
    tests = [test_mutex_blocks_video_while_llm_resident,
             test_mutex_releases_once_llm_gone,
             test_llm_memory_counts_against_budget,
             test_no_llm_module_behaviour_unchanged,
             test_chat_forwards_to_upstream,
             test_chat_503_while_video_running,
             test_chat_rejects_stream,
             test_request_unload_defers_while_busy,
             test_unload_with_no_server_is_noop_true]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed")
