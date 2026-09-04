"""Tests for server/workers/tts_qwen.py — stubbed CLI, no GPU.

Run: .venv/bin/python tests/test_tts_qwen.py
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# must import before stub env vars are set
from server.workers import tts_qwen  # noqa: E402

WAV = (b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + struct.pack(
    "<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16) + b"data" + struct.pack("<I", 4)
    + b"\x00\x00\x00\x00")

# stub CLI: writes argv to $STUB_ARGV, optional sleep via $STUB_SLEEP, emits wav
STUB = """
import json, os, sys, time
args = sys.argv[1:]
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if os.environ.get("STUB_ARGV"):
    open(os.environ["STUB_ARGV"], "w").write(json.dumps(args))
with open(args[args.index("--output_path") + 1] + "/output.wav", "wb") as f:
    f.write(%r)
""" % WAV


def _stub(tmp: str) -> str:
    p = Path(tmp) / "stub_tts.py"
    p.write_text(f"#!{sys.executable}\n{STUB}")
    p.chmod(0o755)
    return str(p)


def test_tts_qwen_builds_command_and_finds_output():
    with tempfile.TemporaryDirectory() as tmp:
        stub = _stub(tmp)
        saved = {k: os.environ.get(k) for k in
                 ("MLXAUDIO_BIN", "MLXAUDIO_MODEL", "STUB_ARGV", "STUB_SLEEP")}
        os.environ["MLXAUDIO_BIN"] = stub
        os.environ["MLXAUDIO_MODEL"] = "fake/qwen-tts"
        os.environ["STUB_ARGV"] = str(Path(tmp) / "argv.json")
        os.environ.pop("STUB_SLEEP", None)
        try:
            job_dir = Path(tmp) / "job"
            job_dir.mkdir()
            meta = tts_qwen.run(
                {"text": "你好世界", "voice": "Vivian", "speed": 1.1,
                 "lang_code": "Chinese"},
                job_dir, lambda *a: None, lambda: False)
            args = json.loads((Path(tmp) / "argv.json").read_text())
            assert args[args.index("--text") + 1] == "你好世界"
            assert args[args.index("--voice") + 1] == "Vivian"
            assert args[args.index("--speed") + 1] == "1.1"
            assert args[args.index("--lang_code") + 1] == "Chinese"
            assert args[args.index("--output_path") + 1] == str(job_dir)
            assert args[args.index("--model") + 1] == "fake/qwen-tts"
            assert meta["output_path"] == str(job_dir / "output.wav")
            assert meta["sample_rate"] == 24000
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_tts_qwen_errors():
    with tempfile.TemporaryDirectory() as tmp:
        stub = _stub(tmp)
        saved = {k: os.environ.get(k) for k in
                 ("MLXAUDIO_BIN", "MLXAUDIO_MODEL", "STUB_SLEEP")}
        os.environ["MLXAUDIO_BIN"] = stub
        os.environ["MLXAUDIO_MODEL"] = "fake/qwen-tts"
        try:
            job_dir = Path(tmp) / "job"
            job_dir.mkdir()
            for params, msg in [
                ({}, "text is required"),
                ({"text": "x", "ref_audio": "/no/a.wav"}, "ref_audio not found"),
            ]:
                try:
                    tts_qwen.run(params, job_dir, lambda *a: None, lambda: False)
                    raise AssertionError(f"expected ValueError for {params}")
                except ValueError as e:
                    assert msg in str(e)

            os.environ["STUB_SLEEP"] = "5"  # timeout kills the sleeping stub
            t0 = time.time()
            try:
                tts_qwen.run({"text": "x", "timeout": 0.2}, job_dir,
                             lambda *a: None, lambda: False)
                raise AssertionError("expected timeout")
            except Exception as e:
                assert "timeout" in str(e) and time.time() - t0 < 3

            bad = Path(tmp) / "bad.py"  # nonzero exit -> stderr tail in error
            bad.write_text(f"#!{sys.executable}\n"
                           "import sys; sys.stderr.write('boom'); sys.exit(3)")
            bad.chmod(0o755)
            os.environ["MLXAUDIO_BIN"] = str(bad)
            try:
                tts_qwen.run({"text": "x"}, job_dir, lambda *a: None, lambda: False)
                raise AssertionError("expected CLI failure")
            except Exception as e:
                assert "boom" in str(e)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
