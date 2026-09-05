#!/usr/bin/env python3
"""Tests for server/workers/image.py — no GPU; iris CLI replaced by a stub.

Run: .venv/bin/python tests/test_image.py
"""
import json
import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server.workers import image  # noqa: E402

# Stub "iris" binary: parses the real CLI flags, writes a valid WxH PNG,
# records its argv next to the output, optionally sleeps (timeout test).
STUB = r'''#!/usr/bin/env python3
import json, os, struct, sys, time, zlib
args = sys.argv[1:]
sleep = os.environ.get("IRIS_STUB_SLEEP")
if sleep:
    time.sleep(float(sleep))
def val(flag):
    return args[args.index(flag) + 1] if flag in args else None
out, w, h = val("-o"), int(val("-W")), int(val("-H"))
raw = b"".join(b"\x00" + b"\x40\x80\xc0" * w for _ in range(h))
def chunk(t, d):
    c = t + d
    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
open(out, "wb").write(
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
open(out + ".args.json", "w").write(json.dumps(args))
print("iris stub ok")
'''


def setup_env(tmp):
    stub = Path(tmp) / "iris_stub.py"
    stub.write_text(STUB)
    stub.chmod(0o755)
    model = Path(tmp) / "model"
    model.mkdir()
    os.environ["IRIS_BIN"] = str(stub)
    os.environ["IRIS_MODEL_DIR"] = str(model)
    return stub, model


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, ratio, phase=""):
        self.calls.append((ratio, phase))


def test_run_args_and_output():
    with tempfile.TemporaryDirectory() as tmp:
        stub, model = setup_env(tmp)
        ref = Path(tmp) / "ref.png"
        ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)  # existence check only
        job = Path(tmp) / "job"
        job.mkdir()
        rec = Recorder()
        meta = image.run(
            {"prompt": "a cat", "width": 64, "height": 32, "steps": 2,
             "seed": 7, "input": str(ref)},
            job, rec, lambda: False)
        out = job / "image.png"
        assert meta["output_path"] == str(out)
        assert (meta["width"], meta["height"]) == (64, 32)
        assert meta["seed"] == 7 and meta["steps"] == 2
        assert out.is_file() and out.stat().st_size > 0
        args = json.loads((job / "image.png.args.json").read_text())
        assert args == ["-d", str(model), "-p", "a cat", "--seed", "7",
                        "--steps", "2", "-W", "64", "-H", "32",
                        "-o", str(out), "-i", str(ref)]
        assert rec.calls[0] == (0.1, "loading")
        assert rec.calls[-1] == (0.95, "saving")


def test_defaults_and_random_seed():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        job = Path(tmp) / "job"
        job.mkdir()
        meta = image.run({"prompt": "x"}, job, Recorder(), lambda: False)
        args = json.loads((job / "image.png.args.json").read_text())
        assert "-i" not in args  # no img2img flag
        assert args[args.index("-W") + 1] == "1024"
        assert args[args.index("-H") + 1] == "1024"
        assert args[args.index("--steps") + 1] == "12"
        assert isinstance(meta["seed"], int) and 0 <= meta["seed"] < 2**31
        assert args[args.index("--seed") + 1] == str(meta["seed"])


def test_missing_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        try:
            image.run({}, Path(tmp), Recorder(), lambda: False)
        except ValueError as e:
            assert "prompt" in str(e)
        else:
            raise AssertionError("expected ValueError for missing prompt")


def test_missing_input():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        try:
            image.run({"prompt": "x", "input": str(Path(tmp) / "nope.png")},
                      Path(tmp), Recorder(), lambda: False)
        except ValueError as e:
            assert "input" in str(e)
        else:
            raise AssertionError("expected ValueError for missing input")


def test_cancel_before_launch():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        job = Path(tmp) / "job"
        job.mkdir()
        try:
            image.run({"prompt": "x"}, job, Recorder(), lambda: True)
        except Exception as e:
            assert "cancel" in str(e).lower()
        else:
            raise AssertionError("expected cancellation")
        assert not (job / "image.png.args.json").exists()  # stub never ran


def test_png_size_rejects_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.png"
        bad.write_bytes(b"not a png at all........")
        try:
            image._png_size(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for non-PNG")


def test_timeout_kills_subprocess():
    with tempfile.TemporaryDirectory() as tmp:
        setup_env(tmp)
        os.environ["IRIS_STUB_SLEEP"] = "30"
        try:
            t0 = time.time()
            try:
                image.run({"prompt": "x", "timeout": 1}, Path(tmp),
                          Recorder(), lambda: False)
            except Exception as e:
                assert "timeout" in str(e)
            else:
                raise AssertionError("expected timeout")
            assert time.time() - t0 < 10  # child killed, not awaited 30s
        finally:
            os.environ.pop("IRIS_STUB_SLEEP", None)


def main():
    tests = [test_run_args_and_output, test_defaults_and_random_seed,
             test_missing_prompt, test_missing_input, test_cancel_before_launch,
             test_png_size_rejects_garbage, test_timeout_kills_subprocess]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
