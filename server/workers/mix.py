"""Mix worker: ffmpeg — sfx/music tracks onto a video (-c:v copy, aac audio).

tracks: [{"sfx_tag": "wind" | "path": "/abs/file.mp3", "gain_db": 0.0, "start_s": 0.0}]
sfx_tag picks a random file from MG_ASSETS/sfx/<tag>/. Empty tracks strips audio.
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import threading
from pathlib import Path

from .. import core

TYPE = "mix"
MEM_GB = 0.5

DEFAULT_TIMEOUT = 600.0
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"  # launchd PATH misses homebrew
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# ponytail: one global lock serializes ffmpeg subprocesses — parallel ffmpeg is
# unmeasured; drop once concurrent mix throughput is measured (see image.py).
_run_lock = threading.Lock()


def _pick_sfx(tag: str) -> str:
    d = core.ASSET_ROOT / "sfx" / tag
    files = sorted(p for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in _AUDIO_EXTS) if d.is_dir() else []
    if not files:
        raise ValueError(f"no sfx files for tag {tag!r} ({d})")
    return str(random.choice(files))


def _video_duration(path: str) -> float:
    probe = shutil.which("ffprobe") or FFMPEG.replace("ffmpeg", "ffprobe")
    out = subprocess.run([probe, "-v", "error",
                          "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=30)
    return float(out.stdout.strip())


def _build_cmd(video_path: str, tracks: list[dict], out: Path,
               duration: float | None = None) -> list[str]:
    cmd = [FFMPEG, "-y", "-v", "error", "-i", video_path]
    for t in tracks:
        cmd += ["-i", t["path"]]
    fc = "".join(
        f"[{i + 1}:a]adelay={int(float(t.get('start_s', 0.0) or 0.0) * 1000)}:all=1,"
        f"volume={float(t.get('gain_db', 0.0))}dB[a{i}];"
        for i, t in enumerate(tracks))
    if fc:
        cmd += ["-filter_complex",
                fc + "".join(f"[a{i}]" for i in range(len(tracks)))
                + f"amix=inputs={len(tracks)}:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac"]
    else:  # no tracks: strip audio
        cmd += ["-an", "-c:v", "copy"]
    if duration:
        # amix runs to the longest track; long ambience sfx would stretch the
        # mp4 with a frozen-video tail — cap the output at the video duration
        cmd += ["-t", f"{duration:.3f}"]
    return cmd + [str(out)]


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    video_path = params.get("video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"video_path not found: {video_path}")
    tracks = params.get("tracks") or []
    if not isinstance(tracks, list):
        raise ValueError("tracks must be a list")
    for t in tracks:
        if not isinstance(t, dict):
            raise ValueError("tracks entries must be objects")
        if not (t.get("path") or t.get("sfx_tag")):
            raise ValueError("track needs path or sfx_tag")
    resolved = []
    for t in tracks:
        t = dict(t)
        if t.get("sfx_tag"):
            t["path"] = _pick_sfx(t["sfx_tag"])
        if not t.get("path") or not Path(t["path"]).is_file():
            raise ValueError(f"track file not found: {t.get('path')}")
        resolved.append(t)

    out = job_dir / "output.mp4"
    if cancel():
        raise Exception("cancelled")
    progress(0.1, "mixing")
    try:
        duration = _video_duration(video_path)
    except Exception:  # noqa: BLE001 — probe failed: no cap (old behaviour)
        duration = None
    with _run_lock:
        try:
            proc = subprocess.run(
                _build_cmd(video_path, resolved, out, duration), capture_output=True, text=True,
                timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            raise Exception(f"mix timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
    progress(0.95, "saving")
    if proc.returncode != 0:
        raise Exception(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '')[-500:]}")
    if not out.is_file():
        raise Exception("mix produced no output")
    return {"output_path": str(out), "tracks": len(resolved)}
