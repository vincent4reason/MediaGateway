"""Video worker: h3.c (MiniMax-H3) via vendor/h3_bridge.py.

Resident engine singleton; MEM_GB fills the whole budget so the scheduler
never co-schedules anything else on the GPU. h3_bridge chdirs to the dylib
dir on load (shaders resolve relative to cwd) — that is by design, not a bug.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from vendor.h3_bridge import H3Engine

TYPE = "video"
MEM_GB = 35.0

LIB = os.environ.get("H3C_LIBRARY", "/Users/vincent/tool/h3.c/libh3.dylib")
MODEL_DIR = os.environ.get("H3C_MODEL_DIR", "/Users/vincent/tool/h3.c/MiniMax-H3")

_REF_KINDS = {"image", "video", "audio", "video_audio"}


class VideoError(Exception):
    pass


_engine = None  # tests inject a fake here; first run() loads the real one
_gen_lock = threading.Lock()  # defensive: scheduler already gives video exclusive memory


def _get_engine():
    global _engine
    if _engine is None:
        eng = H3Engine(LIB, MODEL_DIR)
        try:
            eng.load()
        except Exception as e:  # noqa: BLE001
            raise VideoError(f"h3 engine load failed ({LIB}): {e}") from e
        _engine = eng
    return _engine


def _round32(v) -> int:
    return max(32, int(v) // 32 * 32)


def _check_file(label: str, path):
    if not path or not os.path.isfile(path):
        raise VideoError(f"{label} not found: {path}")
    return path


def _build_refs(refs) -> list:
    out = []
    for r in refs or []:
        kind = r.get("kind")
        if kind not in _REF_KINDS:
            raise VideoError(f"bad ref kind: {kind!r} (want one of {sorted(_REF_KINDS)})")
        out.append({
            "kind": kind,
            "path": _check_file("ref file", r.get("path")),
            "audio_path": _check_file("ref audio_path", r.get("audio_path"))
            if r.get("audio_path") else None,
            "include_embedded_audio": bool(r.get("include_embedded_audio", False)),
        })
    return out


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise VideoError("prompt is required")

    overrides = dict(
        width=_round32(params.get("width", 864)),
        height=_round32(params.get("height", 480)),
        steps=int(params.get("steps", 6)),
        denoise_reuse=int(params.get("denoise_reuse", 1)),
        dit_layers=int(params.get("dit_layers", 45)),
    )
    # core_reuse/token_reduction/ssd_streaming mirror h3cweb: only send non-defaults.
    if int(params.get("core_reuse", 1)) > 1:
        overrides["core_reuse"] = int(params["core_reuse"])
    if params.get("token_reduction"):
        overrides["token_reduction"] = 1
    if params.get("ssd_streaming"):
        overrides["ssd_streaming"] = 1
    # 1 second = 24 frames; seconds wins over frames (same as h3cweb).
    if params.get("seconds"):
        overrides["frames"] = max(1, round(float(params["seconds"]) * 24))
    elif params.get("frames"):
        overrides["frames"] = int(params["frames"])
    if params.get("seed") is not None:
        overrides["seed"] = int(params["seed"])
    if params.get("first_frame"):
        overrides["first_frame"] = _check_file("first_frame", params["first_frame"])
    if params.get("last_frame"):
        overrides["last_frame"] = _check_file("last_frame", params["last_frame"])
    if params.get("reference_image_size") is not None:
        overrides["reference_image_size"] = int(params["reference_image_size"])

    refs = _build_refs(params.get("refs"))
    output_path = str(job_dir / "output.mp4")

    def on_progress(phase, done, total):
        # h3.c has no interrupt API; raising here only propagates if the caller
        # is Python (tests). With the real ctypes engine the exception is
        # swallowed by the callback trampoline, so we also check cancel() after
        # generate returns — the engine may finish the current generation, accepted.
        if cancel():
            raise VideoError("cancelled")
        progress(done / total if total else 0.0, phase)

    # Release the 35GB engine after the job unless the caller explicitly asks
    # to keep it resident (batch rendering). The scheduler's memory budget only
    # counts *running* jobs — a resident engine would be invisible 35GB, so
    # unload-by-default is what keeps the budget honest (doc §31 释放 Worker).
    # close() also restores the process cwd that load() chdir'd away.
    global _engine
    with _gen_lock:
        engine = _get_engine()
        try:
            meta = engine.generate(
                prompt, output_path=output_path, refs=refs,
                on_progress=on_progress, **overrides)
        finally:
            if not params.get("keep_loaded"):
                engine.close()
                _engine = None
    if cancel():
        raise VideoError("cancelled")
    return {"output_path": output_path,
            **{k: meta[k] for k in ("width", "height", "frames", "fps", "seed")
               if k in meta}}
