#!/usr/bin/env python3
"""Voice worker tests — stub :8001 with stdlib http.server, no GPU needed.
Run: .venv/bin/python tests/test_voice.py
"""
import base64
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from server.workers import voice as vw  # noqa: E402
from cosyvoice import client as cc  # noqa: E402

cc.RETRY_DELAY_S = 0.0  # 失败路径测试不求慢

WAV = b"RIFF\x00\x00stub"
PROG = []


class Stub(BaseHTTPRequestHandler):
    scenario = "ok"  # ok | bad400 | err500
    hits = 0
    last_payload = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        Stub.hits += 1
        Stub.last_payload = json.loads(
            self.rfile.read(int(self.headers["Content-Length"])))
        if Stub.scenario == "ok":
            body = {"ok": True, "wav_b64": base64.b64encode(WAV).decode(),
                    "sample_rate": 24000}
            self._send(200, body)
        elif Stub.scenario == "bad400":
            self._send(400, {"detail": "prompt_text 需含 <|endofprompt|>"})
        else:
            self._send(500, {"detail": "boom"})

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_worker(params, job_dir):
    PROG.clear()
    return vw.run(params, job_dir,
                  progress=lambda r, p="": PROG.append((r, p)),
                  cancel=lambda: False)


def fresh_job_dir():
    d = Path(tempfile.mkdtemp())
    return d


def with_stub(scenario, fn):
    Stub.scenario = scenario
    Stub.hits = 0
    Stub.last_payload = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        return fn(url)
    finally:
        srv.shutdown()


def test_ok_generates_file(url):
    params = {"text": "你到底想点啊？", "voice": "C001", "speed": 1.1,
              "base_url": url}
    res = run_worker(params, fresh_job_dir())
    out = res["output_path"]
    assert out.endswith("dialogue.wav"), out
    assert Path(out).read_bytes() == WAV
    assert res["sample_rate"] == 24000
    v = json.loads((ROOT / "vendor/cosyvoice/voices.json").read_text())["C001"]
    p = Stub.last_payload
    assert p["text"] == "你到底想点啊？"
    assert p["speed"] == 1.1
    assert p["prompt_wav"] == v["prompt_wav"]      # voices.json 解析正确
    assert p["prompt_text"] == v["prompt_text"]
    assert "<|endofprompt|>" in p["prompt_text"]
    assert Stub.hits == 1
    assert PROG == [(0.1, "synthesizing")], PROG


def test_4xx_no_retry(url):
    params = {"text": "hi", "voice": "C001", "base_url": url}
    try:
        run_worker(params, fresh_job_dir())
    except RuntimeError as e:  # SystemExit 必须已被转成普通异常
        assert "400" in str(e), str(e)
    else:
        raise AssertionError("expected RuntimeError")
    assert Stub.hits == 1  # 4xx 不重试


def test_5xx_retries_then_plain_exception(url):
    try:
        run_worker({"text": "hi", "voice": "C001", "base_url": url},
                   fresh_job_dir())
    except RuntimeError as e:
        assert "重试" in str(e) or "均失败" in str(e), str(e)
    else:
        raise AssertionError("expected RuntimeError")
    assert Stub.hits == cc.ATTEMPTS


def test_unknown_voice_plain_exception(url):
    try:
        run_worker({"text": "hi", "voice": "NOPE", "base_url": url},
                   fresh_job_dir())
    except RuntimeError as e:
        assert "voice 未注册" in str(e), str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_missing_params(url):
    for params in ({}, {"text": "  "},
                   {"text": "hi"}):  # 无 voice 也无 prompt 对
        try:
            run_worker(params, fresh_job_dir())
        except (ValueError, RuntimeError) as e:
            assert not isinstance(e, SystemExit)
        else:
            raise AssertionError(f"expected error for {params}")


if __name__ == "__main__":
    tests = [test_ok_generates_file, test_4xx_no_retry,
             test_5xx_retries_then_plain_exception,
             test_unknown_voice_plain_exception, test_missing_params]
    for t in tests:
        with_stub("ok" if t is test_ok_generates_file else
                  ("bad400" if t is test_4xx_no_retry else "err500"),
                  lambda url, t=t: t(url))
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed")
