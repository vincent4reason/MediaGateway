"""Tests for server/compat_openai.py — mocked jobs, no GPU.

Run: .venv/bin/python tests/test_compat_openai.py
"""
from __future__ import annotations

import base64
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
