"""Shot composite worker: image → video → voice → music → ffmpeg mux.

Orchestrates the other workers in-process, sequentially, inside one job.
The scheduler sees MEM_GB = max stage (video, 35GB) so a shot render holds
the GPU exclusively for its whole duration — stages never overlap.

params (every stage optional, but at least one required):
    image: {prompt, width?, height?, steps?, seed?, input?}
    video: {prompt, seconds?, steps?, seed?, first_frame?="auto"|path,
            last_frame?, refs?[], ...}   # "auto" = use generated image
    voice: {text, voice|prompt_wav+prompt_text, speed?}
    music: {prompt, duration_s?, seed?, lyrics?}
    mix:   {subtitles?: path, dialogue_volume?: 1.0, music_volume?: 0.15,
            timeout?: 600}
Final output: job_dir/shot.mp4 (skipped if no video stage).
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import render
from . import image as image_w
from . import music as music_w
from . import video as video_w
from . import voice as voice_w

TYPE = "shot"
MEM_GB = video_w.MEM_GB  # peak stage

_STAGE_WEIGHTS = {"image": 1, "video": 4, "voice": 1, "music": 1}


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    enabled = [s for s in ("image", "video", "voice", "music") if params.get(s)]
    if not enabled:
        raise ValueError("at least one of image/video/voice/music is required")
    total_w = sum(_STAGE_WEIGHTS[s] for s in enabled)
    done_w = 0.0
    meta: dict = {}

    def stage_cb(name):
        def cb(ratio, phase=""):
            ratio = ratio if isinstance(ratio, (int, float)) else 0.0
            p = (done_w + _STAGE_WEIGHTS[name] * min(ratio, 1.0)) / total_w
            progress(min(p, 0.99), f"{name}:{phase}".strip(":"))
        return cb

    outputs: dict = {}
    for stage in enabled:
        if cancel():
            raise Exception("cancelled")
        spec = dict(params[stage])
        out_dir = job_dir / stage
        out_dir.mkdir(parents=True, exist_ok=True)

        if stage == "video" and spec.get("first_frame") == "auto":
            img = outputs.get("image")
            if not img:
                raise ValueError("video.first_frame='auto' needs the image stage")
            spec["first_frame"] = img["output_path"]

        module = {"image": image_w, "video": video_w,
                  "voice": voice_w, "music": music_w}[stage]
        outputs[stage] = module.run(spec, out_dir, stage_cb(stage), cancel)
        meta[stage] = outputs[stage]
        done_w += _STAGE_WEIGHTS[stage]
        progress(min(done_w / total_w, 0.99), stage)

    vid = outputs.get("video")
    if not vid:
        first = enabled[0]
        return {"output_path": outputs[first]["output_path"], "stages": meta,
                "note": "no video stage — returning first stage output"}

    mix = params.get("mix") or {}
    tracks = []
    if "voice" in outputs:  # dialogue track → mux's dialogue_volume default
        tracks.append({"path": outputs["voice"]["output_path"], "start": 0})
    if "music" in outputs:  # bed track → mux's music_volume default, loops to video length
        tracks.append({"path": outputs["music"]["output_path"], "loop": True})
    final = str(job_dir / "shot.mp4")
    render.mux(
        video=vid["output_path"], audio_tracks=tracks, output=final,
        subtitles=mix.get("subtitles"),
        dialogue_volume=float(mix.get("dialogue_volume", 1.0)),
        music_volume=float(mix.get("music_volume", 0.15)),
        timeout=float(mix.get("timeout", 600)))
    return {"output_path": final, "stages": meta}
