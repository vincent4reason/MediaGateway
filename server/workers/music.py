"""Music worker: ACE-Step 1.5 CLI (acestep-v15-turbo DiT via MLX/MPS; LM off).

Measured: 45s wav / 8 steps ≈ 128s cold (weight page-cache + MLX compile),
17s warm; peak RSS 14.8GB (/usr/bin/time -l; phys_footprint reports ~47GB
because MLX/Metal unified-memory buffers are attributed to the process).
Output: 48kHz/16bit/stereo wav. Env overrides: MUSIC_PYTHON, MUSIC_SCRIPT
(defaults under /Users/vincent/tool/ace-step).
"""
from __future__ import annotations

import json
import os
import random
import struct
import subprocess
import threading
from pathlib import Path

TYPE = "music"
MEM_GB = 15.0  # measured peak RSS 14.8GB (45s / 8 steps; MLX DiT on MPS)

DEFAULT_PYTHON = "/Users/vincent/tool/ace-step/.venv/bin/python"
DEFAULT_SCRIPT = "/Users/vincent/tool/ace-step/mg_music.py"
DEFAULT_TIMEOUT = 900.0

# ponytail: one global lock serializes music subprocesses — parallel music+music
# is unmeasured; narrow once parallel throughput is measured (see image.py).
_run_lock = threading.Lock()


def _wav_info(path: Path) -> dict:
    """Parse sample_rate/channels/bits/duration from a RIFF/WAVE header."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"output is not a valid WAV: {path}")
        rate = channels = bits = None
        data_size = 0
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                channels, rate = struct.unpack("<HI", fmt[2:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
            elif cid == b"data":
                data_size = size
                f.seek(size, os.SEEK_CUR)
            else:
                f.seek(size + (size & 1), os.SEEK_CUR)
    if not (rate and channels and bits and data_size):
        raise ValueError(f"incomplete WAV header: {path}")
    return {"sample_rate": rate, "channels": channels, "bits": bits,
            "duration": round(data_size / (rate * channels * bits // 8), 2)}


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("params.prompt is required")
    duration = float(params.get("duration_s", 45))
    if not 10 <= duration <= 600:  # ACE-Step 1.5 supported range
        raise ValueError(f"duration_s must be 10-600, got {duration}")
    seed = params.get("seed")
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    lyrics = (params.get("lyrics") or "").strip()
    timeout = float(params.get("timeout", DEFAULT_TIMEOUT))

    out = job_dir / "output.wav"
    script = os.environ.get("MUSIC_SCRIPT", DEFAULT_SCRIPT)
    cmd = [
        os.environ.get("MUSIC_PYTHON", DEFAULT_PYTHON),
        script,
        "--prompt", str(prompt),
        "--duration", str(duration),
        "--seed", str(seed),
        "--out", str(out),
    ]
    if lyrics:
        cmd += ["--lyrics", lyrics]

    if cancel():
        raise Exception("cancelled")

    progress(0.1, "loading")
    # HF_HUB_OFFLINE: weights are pre-downloaded; keep the child from stalling on
    # hub checks. Proxies are dropped too (local pip/hf proxy 502s on this host).
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)

    with _run_lock:
        try:
            proc = subprocess.run(
                cmd, cwd=str(Path(script).resolve().parent),
                capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise Exception(f"music timeout after {timeout}s (process killed)")
    progress(0.95, "saving")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise Exception(f"music exited {proc.returncode}: {tail}")
    if not out.is_file():
        raise Exception(f"music produced no output: {(proc.stderr or '')[-500:]}")
    info = _wav_info(out)
    result_seed = seed
    for line in (proc.stdout or "").splitlines():  # "MG_RESULT {json}" from wrapper
        if line.startswith("MG_RESULT "):
            payload = json.loads(line[len("MG_RESULT "):])
            if payload.get("ok") and payload.get("seed") is not None:
                result_seed = payload["seed"]
    meta = {"duration": info["duration"], "sample_rate": info["sample_rate"],
            "channels": info["channels"], "seed": result_seed,
            "lyrics": bool(lyrics), "duration_s": duration}
    return {"output_path": str(out), **meta}
