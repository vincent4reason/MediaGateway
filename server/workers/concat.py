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
    render.concat(shots, output,
                  transition=str(params.get("transition", "")),
                  duration=float(params.get("transition_duration", 0.5)))
    progress(1.0, "done")
    return {"output_path": output, "count": len(shots)}
