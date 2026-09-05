"""Image worker: iris.c CLI (flux-klein-4b, Metal GPU) — txt2img / img2img.

Measured (docs/tools.md): 1024px / 4 steps ≈ 67s, 10.8GB RSS.
Phase 5 calls the CLI via subprocess; libiris.dylib FFI is a later optimization.
Env overrides: IRIS_BIN, IRIS_MODEL_DIR (defaults under /Users/vincent/tool/iris.c).
"""
from __future__ import annotations

import os
import random
import struct
import subprocess
import threading
from pathlib import Path

TYPE = "image"
MEM_GB = 11.0

DEFAULT_BIN = "/Users/vincent/tool/iris.c/iris"
DEFAULT_MODEL = "/Users/vincent/tool/iris.c/flux-klein-4b"
DEFAULT_TIMEOUT = 600.0

# ponytail: one global lock serializes iris subprocesses — parallel iris+iris is
# unmeasured (docs only prove iris+cosyvoice coexists), so this is the safe fallback
# even though the budget admits 2 (2×10.8GB < 40GB). Narrow to the model-load window
# or drop it once parallel throughput is measured.
_run_lock = threading.Lock()


def _png_size(path: Path) -> tuple[int, int]:
    """Read width/height from the PNG IHDR header (no PIL dependency)."""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ValueError(f"output is not a valid PNG: {path}")
    return struct.unpack(">II", head[16:24])


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("params.prompt is required")
    width = int(params.get("width", 1024))
    height = int(params.get("height", 1024))
    steps = int(params.get("steps", 12))  # 4 步出片偏卡通/油膩感，寫實檔 12 步（512 圖約 1 分鐘）
    seed = params.get("seed", 42)
    if seed is None:  # CLI requires an explicit --seed; pick one for "random"
        seed = random.randint(0, 2**31 - 1)
    ref = params.get("input")
    if ref is not None and not isinstance(ref, (str, list)):
        raise ValueError("input must be a path string or list of paths")
    refs = [ref] if isinstance(ref, str) else list(ref or [])
    if len(refs) > 16:  # iris MAX_INPUT_IMAGES
        raise ValueError("at most 16 reference images supported")
    for r in refs:
        if not Path(r).is_file():
            raise ValueError(f"input image not found: {r}")
    timeout = float(params.get("timeout", DEFAULT_TIMEOUT))

    out = job_dir / "image.png"
    model = os.environ.get("IRIS_MODEL_DIR", DEFAULT_MODEL)
    cmd = [
        os.environ.get("IRIS_BIN", DEFAULT_BIN),
        "-d", model,
        "-p", prompt,
        "--seed", str(seed),
        "--steps", str(steps),
        "-W", str(width),
        "-H", str(height),
        "-o", str(out),
    ]
    for r in refs:
        cmd += ["-i", str(r)]

    # cancel() is checked once before launch; the running subprocess cannot be
    # interrupted — cancellation takes effect at the next scheduling round.
    if cancel():
        raise Exception("cancelled")

    progress(0.1, "loading")  # iris has no progress callback; coarse two-phase only
    with _run_lock:
        try:
            proc = subprocess.run(
                cmd, cwd=str(Path(model).resolve().parent),
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Exception(f"iris timeout after {timeout}s (process killed)")
    progress(0.95, "saving")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise Exception(f"iris exited {proc.returncode}: {tail}")
    if not out.is_file():
        raise Exception(f"iris produced no output: {(proc.stderr or '')[-500:]}")
    w, h = _png_size(out)
    return {"output_path": str(out), "width": w, "height": h,
            "steps": steps, "seed": seed}
