"""Tests for server/compat_openai.py — mocked jobs, no GPU.

Run: .venv/bin/python tests/test_compat_openai.py
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离：绝不碰真实 data/gateway.db 与真实 ：8000 LLM（与 test_compat 同款）
os.environ.setdefault("MG_DB", os.path.join(tempfile.mkdtemp(prefix="mgtest_"), "gateway.db"))
os.environ.setdefault("QWEN_PORT", "8899")

from fastapi.testclient import TestClient  # noqa: E402

from server import compat_openai, core  # noqa: E402
from server.main import app  # noqa: E402


def _png_bytes() -> bytes:
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    import zlib
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


class FakeJob:
    """Replaces core.create_job with an instantly-completed job."""

    def __init__(self, output_file):
        self.output_file = output_file
        self.calls = []
        self.counter = 0

    def create(self, jtype, params, priority=0):
        self.counter += 1
        self.calls.append((jtype, params))
        jid = f"fake_{self.counter:04d}"
        if not Path(self.output_file).exists():  # don't clobber caller-provided files
            Path(self.output_file).write_bytes(_png_bytes() if jtype == "image" else b"RIFF")
        return {"id": jid, "type": jtype, "status": "completed", "progress": 1.0,
                "output_path": self.output_file, "error": None}

    def get(self, job_id):
        return {"id": job_id, "type": "fake", "status": "completed", "progress": 1.0,
                "output_path": self.output_file, "error": None}


def test_image_generations_returns_b64():
    with tempfile.TemporaryDirectory() as d:
        png = str(Path(d) / "out.png")
        fake = FakeJob(png)
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/images/generations", json={
                "prompt": "a cat", "size": "864x480", "seed": 42})
            assert r.status_code == 200, r.text
            data = r.json()["data"][0]["b64_json"]
            assert base64.b64decode(data)[:8] == b"\x89PNG\r\n\x1a\n"
            assert fake.calls[0][0] == "image"
            assert fake.calls[0][1]["width"] == 864 and fake.calls[0][1]["height"] == 480
            assert fake.calls[0][1]["seed"] == 42
        finally:
            core.create_job, core.get_job = saved


def test_image_size_rounding_and_bad_size():
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.png"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            client.post("/v1/images/generations", json={"prompt": "x", "size": "853x499"})
            assert (fake.calls[0][1]["width"], fake.calls[0][1]["height"]) == (832, 480)
            r = client.post("/v1/images/generations", json={"prompt": "x", "size": "big"})
            assert r.status_code == 400
        finally:
            core.create_job, core.get_job = saved


def test_image_edits_passes_references():
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.png"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/images/edits",
                            data={"prompt": "a cat holding it", "size": "1024x1024"},
                            files=[("image", ("a.png", _png_bytes(), "image/png")),
                                   ("image", ("b.png", _png_bytes(), "image/png"))])
            assert r.status_code == 200, r.text
            assert base64.b64decode(r.json()["data"][0]["b64_json"])[:8] == b"\x89PNG\r\n\x1a\n"
            refs = fake.calls[0][1]["input"]
            assert len(refs) == 2 and refs[0].endswith("ref_0.png") and refs[1].endswith("ref_1.png")
            # jpeg magic passes through with .jpg suffix; garbage (webp/heic-like) -> ffmpeg -> 400
            r = client.post("/v1/images/edits", data={"prompt": "x"},
                            files=[("image", ("c.jpg", b"\xff\xd8\xff\xe0jpegbody", "image/jpeg"))])
            assert r.status_code == 200, r.text
            assert fake.calls[1][1]["input"][0].endswith("ref_0.jpg")
            r = client.post("/v1/images/edits", data={"prompt": "x"},
                            files=[("image", ("d.webp", b"RIFFxxxxWEBP garbage", "image/webp"))])
            assert r.status_code == 400, r.text
        finally:
            core.create_job, core.get_job = saved


def test_image_edits_rejects_bad_input():
    client = TestClient(app)
    r = client.post("/v1/images/edits", data={"prompt": "x"})
    assert r.status_code == 400
    r = client.post("/v1/images/edits", data={"prompt": "x"},
                    files=[("image", ("a.png", _png_bytes(), "image/png")),
                           ("mask", ("m.png", _png_bytes(), "image/png"))])
    assert r.status_code == 400


def test_image_worker_validates_references():
    from server.workers import image as image_worker
    with tempfile.TemporaryDirectory() as d:
        for params, msg in [
            ({"prompt": "x", "input": str(Path(d) / "missing.png")}, "input image not found"),
            ({"prompt": "x", "input": ["/no/a.png"] * 17}, "at most 16"),
            ({"prompt": "x", "input": "/no/a.png"}, "input image not found"),
        ]:
            try:
                image_worker.run(params, Path(d), lambda *a: None, lambda: False)
                raise AssertionError(f"expected ValueError for {params}")
            except ValueError as e:
                assert msg in str(e)


def test_audio_speech_returns_url_and_content():
    with tempfile.TemporaryDirectory() as d:
        wav = str(Path(d) / "o.wav")
        Path(wav).write_bytes(b"RIFFxxxxWAVE")
        fake = FakeJob(wav)
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/audio/speech", json={"input": "你好", "voice": "C001"})
            assert r.status_code == 200, r.text
            url = r.json()["url"]
            assert url.endswith("/content"), url
            jid = fake.calls[0][1] and url.rsplit("/", 2)[-2]
            c = client.get(f"/v1/audio/jobs/{jid}/content")
            assert c.status_code == 200 and c.content == b"RIFFxxxxWAVE", f"status={c.status_code} body={c.content[:40]!r} url={url}"
        finally:
            core.create_job, core.get_job = saved


def test_audio_speech_model_falls_back_to_voice():
    """No voice + model in voices.json -> model used as voiceId; unknown model -> default voice."""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.wav"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/audio/speech", json={"input": "你好", "model": "C002"})
            assert r.status_code == 200, r.text
            assert fake.calls[0][1]["voice"] == "C002"
            r = client.post("/v1/audio/speech", json={"input": "你好", "model": "gpt-4o-mini-tts"})
            assert r.status_code == 200, r.text
            # 未命中 model：回退 voices.json 首个音色（无绑定调用方的兜底）
            first_voice = next(iter(json.load(open(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "vendor", "cosyvoice", "voices.json")))))
            assert fake.calls[1][1]["voice"] == first_voice
            # explicit voice still wins over model
            r = client.post("/v1/audio/speech", json={"input": "你好", "model": "C001", "voice": "C002"})
            assert r.status_code == 200, r.text
            assert fake.calls[2][1]["voice"] == "C002"
        finally:
            core.create_job, core.get_job = saved


def test_audio_speech_qwen_routes_to_tts_qwen():
    """model qwen* -> tts_qwen job; plugin-coalesced voice (== model) is dropped."""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.wav"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/audio/speech", json={
                "input": "你好", "model": "qwen3-tts", "voice": "qwen3-tts"})
            assert r.status_code == 200, r.text
            assert fake.calls[0][0] == "tts_qwen"
            assert "voice" not in fake.calls[0][1]
            r = client.post("/v1/audio/speech", json={
                "input": "你好", "model": "tts_qwen", "voice": "Vivian"})
            assert r.status_code == 200, r.text
            assert fake.calls[1][0] == "tts_qwen"
            assert fake.calls[1][1]["voice"] == "Vivian"
            # non-qwen model still routes to the cosyvoice voice worker
            r = client.post("/v1/audio/speech", json={"input": "你好", "model": "C002"})
            assert r.status_code == 200, r.text
            assert fake.calls[2][0] == "voice"
        finally:
            core.create_job, core.get_job = saved


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
