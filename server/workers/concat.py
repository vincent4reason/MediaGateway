"""Concat worker: ffmpeg-join ≥2 shot videos into one final mp4."""
from __future__ import annotations

from pathlib import Path

from .. import render

TYPE = "concat"
MEM_GB = 1  # pure ffmpeg CPU


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    shots = params.get("shots")
    if not isinstance(shots, list) or len(shots) < 2:
        raise ValueError("shots must be a list of ≥2 video paths")
    if not all(isinstance(s, str) and s for s in shots):
        raise ValueError("shots entries must be non-empty strings")
    output = str(job_dir / params.get("output_name", "final.mp4"))
    segs = params.get("music_segments")
    if not segs:
        render.concat(shots, output,
                      transition=str(params.get("transition", "")),
                      duration=float(params.get("transition_duration", 0.5)))
    else:
        if not isinstance(segs, list) or not all(
                isinstance(s, dict) and isinstance(s.get("path"), str) and s["path"]
                for s in segs):
            raise ValueError("music_segments must be a list of {path, duration_s}")
        joined = str(job_dir / "_joined.mp4")
        render.concat(shots, joined,
                      transition=str(params.get("transition", "")),
                      duration=float(params.get("transition_duration", 0.5)))
        render.bgm(joined, segs, output,
                   gain_db=float(params.get("bgm_gain_db", -6.0)))
    progress(1.0, "done")
    return {"output_path": output, "count": len(shots)}
