#!/usr/bin/env python3
"""Tests for server/workers/music.py — no GPU; mg_music.py replaced by a stub.

Run: .venv/bin/python tests/test_music.py
"""
import json
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server.workers import music  # noqa: E402

# Stub "mg_music.py": parses the real flags, writes a valid 48k/16bit/stereo wav
# (2s of silence), records its argv next to the output, optionally sleeps
# (timeout test) or exits nonzero (failure test).
STUB = r'''#!/usr/bin/env python3
import json, os, struct, sys, time
args = sys.argv[1:]
sleep = os.environ.get("MUSIC_STUB_SLEEP")
if sleep:
    time.sleep(float(sleep))
if os.environ.get("MUSIC_STUB_FAIL"):
    print("stub boom", file=sys.stderr)
    sys.exit(3)
def val(flag):
    return args[args.index(flag) + 1] if flag in args else None
out = val("--out")
rate, ch, bits = 48000, 2, 16
seconds = float(val("--duration")) / 22.5  # short on purpose
frames = int(rate * seconds)
data = b"\x00\x00" * (ch * frames)
open(out, "wb").write(
    b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    + b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    + b"data" + struct.pack("<I", len(data)) + data)
open(out + ".args.json", "w").write(json.dumps(args))
print("MG_RESULT " + json.dumps({"ok": True, "path": out, "seed": int(val("--seed"))}))
'''


def setup_env(tmp):
    stub = Path(tmp) / "mg_music_stub.py"
    stub.write_text(STUB)
    os.environ["MUSIC_SCRIPT"] = str(stub)
    os.environ["MUSIC_PYTHON"] = sys.executable
    return stub


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, ratio, phase=""):
        self.calls.append((ratio, phase))


def test_run_args_and_output():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        job = Path(tmp) / "job"
        job.mkdir()
        rec = Recorder()
        meta = music.run({"prompt": "cinematic lonely", "duration_s": 45,
                          "seed": 7, "lyrics": "[Verse]\nla"},
                         job, rec, lambda: False)
        out = job / "output.wav"
        assert meta["output_path"] == str(out)
        assert meta["sample_rate"] == 48000 and meta["channels"] == 2
        assert abs(meta["duration"] - 2.0) < 0.01
        assert meta["seed"] == 7  # echoed back via MG_RESULT payload
        assert meta["lyrics"] is True
        assert out.is_file() and out.stat().st_size > 0
        args = json.loads((job / "output.wav.args.json").read_text())
        assert args == ["--prompt", "cinematic lonely", "--duration", "45.0",
                        "--seed", "7", "--out", str(out),
                        "--lyrics", "[Verse]\nla"]
        assert rec.calls[0] == (0.1, "loading")
        assert rec.calls[-1] == (0.95, "saving")


def test_defaults_and_random_seed():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        job = Path(tmp) / "job"
        job.mkdir()
        meta = music.run({"prompt": "bgm"}, job, Recorder(), lambda: False)
        args = json.loads((job / "output.wav.args.json").read_text())
        assert "--lyrics" not in args  # instrumental
        assert args[args.index("--duration") + 1] == "45.0"
        assert isinstance(meta["seed"], int) and 0 <= meta["seed"] < 2**31
        assert args[args.index("--seed") + 1] == str(meta["seed"])
        assert meta["lyrics"] is False


def test_missing_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        try:
            music.run({}, Path(tmp), Recorder(), lambda: False)
        except ValueError as e:
            assert "prompt" in str(e)
        else:
            raise AssertionError("expected ValueError for missing prompt")


def test_bad_duration():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        # 越界时长不再拒单：夹紧到 10-600（mux 的 atrim 会裁回视频长度）
        for bad, clamped in ((5, "10.0"), (601, "600.0")):
            job = Path(tmp) / f"job_{bad}"
            job.mkdir()
            meta = music.run({"prompt": "x", "duration_s": bad}, job,
                             Recorder(), lambda: False)
            args = json.loads((job / "output.wav.args.json").read_text())
            assert args[args.index("--duration") + 1] == clamped, (bad, args)
            assert meta["output_path"].endswith("output.wav")


def test_nonzero_exit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        os.environ["MUSIC_STUB_FAIL"] = "1"
        try:
            music.run({"prompt": "x"}, Path(tmp), Recorder(), lambda: False)
        except Exception as e:
            assert "exited 3" in str(e) and "stub boom" in str(e)
        else:
            raise AssertionError("expected failure on nonzero exit")
        finally:
            os.environ.pop("MUSIC_STUB_FAIL", None)


def test_wav_info_rejects_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.wav"
        bad.write_bytes(b"not a wav at all........")
        try:
            music._wav_info(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for non-WAV")


def test_timeout_kills_subprocess():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        os.environ["MUSIC_STUB_SLEEP"] = "30"
        try:
            t0 = time.time()
            try:
                music.run({"prompt": "x", "timeout": 1}, Path(tmp),
                          Recorder(), lambda: False)
            except Exception as e:
                assert "timeout" in str(e)
            else:
                raise AssertionError("expected timeout")
            assert time.time() - t0 < 10  # child killed, not awaited 30s
        finally:
            os.environ.pop("MUSIC_STUB_SLEEP", None)


def main():
    tests = [test_run_args_and_output, test_defaults_and_random_seed,
             test_missing_prompt, test_bad_duration, test_nonzero_exit,
             test_wav_info_rejects_garbage, test_timeout_kills_subprocess]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
