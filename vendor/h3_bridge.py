#!/usr/bin/env python3
"""ctypes bridge for libh3.dylib (antirez/h3.c).

Loads the Metal-native MiniMax-H3 engine once and generates videos with
full h3_params passthrough (steps, denoise_reuse, references, audio, ...).

Usage as a library:
    engine = H3Engine(lib_path, model_dir)
    engine.load()
    result = engine.generate(prompt, width=864, height=480, frames=48,
                             steps=6, refs=[{"kind": "image", "path": "x.png"}],
                             output_path="out.mp4")
    engine.close()

Usage as a CLI:
    python h3_bridge.py --prompt "..." --ref-image x.png --ref-audio v.wav \
        --width 864 --height 480 --frames 48 --steps 6 --out out.mp4
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import sys

# --- C types mirroring h3.h (must match the checked-out h3.h exactly) ---

H3_REFERENCE_IMAGE = 1
H3_REFERENCE_VIDEO = 2
H3_REFERENCE_AUDIO = 3
H3_REFERENCE_VIDEO_AUDIO = 4

H3_DEFAULT_WIDTH = 864
H3_DEFAULT_HEIGHT = 480
H3_DEFAULT_FRAMES = 56
H3_DEFAULT_STEPS = 20
H3_DEFAULT_DIT_LAYERS = 50


class H3Frame(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("stride", ctypes.c_int),
        ("rgb", ctypes.POINTER(ctypes.c_uint8)),
        ("frame_index", ctypes.c_int),
        ("frame_count", ctypes.c_int),
        ("denoise_step", ctypes.c_int),
        ("denoise_steps", ctypes.c_int),
    ]


class H3Reference(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int),
        ("path", ctypes.c_char_p),
        ("audio_path", ctypes.c_char_p),
        ("include_embedded_audio", ctypes.c_int),
    ]


class H3CacheInfo(ctypes.Structure):
    _fields_ = [
        ("embedding_entries", ctypes.c_size_t),
        ("embedding_bytes", ctypes.c_size_t),
        ("prepared_dit", ctypes.c_int),
        ("video_decoder", ctypes.c_int),
    ]


class H3DeviceInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char * 128),
        ("architecture", ctypes.c_char * 128),
        ("physical_memory", ctypes.c_uint64),
        ("recommended_working_set", ctypes.c_uint64),
        ("max_buffer_length", ctypes.c_uint64),
        ("apple_gpu_family", ctypes.c_int),
        ("metal4", ctypes.c_int),
        ("unified_memory", ctypes.c_int),
    ]


class H3ComponentInfo(ctypes.Structure):
    _fields_ = [
        ("bytes", ctypes.c_uint64),
        ("tensor_bytes", ctypes.c_uint64),
        ("files", ctypes.c_size_t),
        ("tensors", ctypes.c_size_t),
    ]


class H3ModelInfo(ctypes.Structure):
    _fields_ = [
        ("text_encoder", H3ComponentInfo),
        ("fl2va_transformer", H3ComponentInfo),
        ("ref2va_transformer", H3ComponentInfo),
        ("video_vae", H3ComponentInfo),
        ("audio_vae", H3ComponentInfo),
    ]


class H3Result(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("frames", ctypes.c_int),
        ("fps", ctypes.c_int),
        ("sample_rate", ctypes.c_int),
        ("seed", ctypes.c_uint64),
    ]


FRAME_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(H3Frame), ctypes.c_void_p)
PROGRESS_CB = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p
)


class H3Params(ctypes.Structure):
    """Must match h3_params in h3.h field-for-field (order matters)."""
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("frames", ctypes.c_int),
        ("steps", ctypes.c_int),
        ("seed", ctypes.c_uint64),
        ("output_path", ctypes.c_char_p),
        ("first_frame", ctypes.c_char_p),
        ("last_frame", ctypes.c_char_p),
        ("references", ctypes.POINTER(H3Reference)),
        ("reference_count", ctypes.c_size_t),
        ("reference_image_size", ctypes.c_int),
        ("denoise_reuse", ctypes.c_int),
        ("dit_layers", ctypes.c_int),
        ("core_reuse", ctypes.c_int),
        ("token_reduction", ctypes.c_int),
        ("use_int8_row_fc2", ctypes.c_int),
        ("use_reference_rope", ctypes.c_int),
        ("ssd_streaming", ctypes.c_int),
        ("render_width", ctypes.c_int),
        ("render_height", ctypes.c_int),
        ("use_slower_bf16_mlp", ctypes.c_int),
        ("use_slower_bf16_qkv", ctypes.c_int),
        ("use_slower_bf16_attention_output", ctypes.c_int),
        ("use_slower_row_major_attention_output", ctypes.c_int),
        ("use_slower_unfused_int8_inputs", ctypes.c_int),
        ("use_slower_unfused_qkv_rope", ctypes.c_int),
        ("use_slower_scalar_qkv_rms", ctypes.c_int),
        ("use_slower_uncached_int8_scales", ctypes.c_int),
        ("use_slower_dynamic_fc1_k", ctypes.c_int),
        ("use_slower_grouped_quantizer", ctypes.c_int),
        ("preview_denoise", ctypes.c_int),
        ("on_frame", FRAME_CB),
        ("on_progress", PROGRESS_CB),
        ("callback_opaque", ctypes.c_void_p),
    ]


class H3Error(Exception):
    pass


def _default_params() -> dict:
    """Defaults matching H3_PARAMS_DEFAULT in h3.h."""
    return {
        "width": H3_DEFAULT_WIDTH,
        "height": H3_DEFAULT_HEIGHT,
        "frames": H3_DEFAULT_FRAMES,
        "steps": H3_DEFAULT_STEPS,
        "seed": 42,
        "denoise_reuse": 1,
        "dit_layers": H3_DEFAULT_DIT_LAYERS,
        "core_reuse": 1,
        "ssd_streaming": 0,
    }


class H3Engine:
    def __init__(self, lib_path: str, model_dir: str):
        self.lib_path = lib_path
        self.model_dir = model_dir
        self._lib = None
        self._ctx = None
        self._refs: list[H3Reference] = []
        # Keep callback objects alive for the lifetime of the engine:
        # ctypes drops the Python wrapper after the C call returns, and the C
        # side stores the function pointer for the whole generation.
        self._cb_frame = FRAME_CB(self._on_frame)
        self._cb_progress = PROGRESS_CB(self._on_progress)
        self.progress_callback = None  # optional python: f(phase, done, total)
        self.frame_callback = None     # optional python: f(frame)

    # --- lifecycle ---

    def load(self):
        # h3.c resolves h3_shaders.metal relative to the process cwd, so the
        # engine must run with the lib directory as cwd for its whole life.
        self._old_cwd = os.getcwd()
        os.chdir(os.path.dirname(os.path.abspath(self.lib_path)))
        self._lib = ctypes.CDLL(self.lib_path)
        lib = self._lib

        lib.h3_load_dir.restype = ctypes.c_void_p
        lib.h3_load_dir.argtypes = [ctypes.c_char_p]
        lib.h3_free.argtypes = [ctypes.c_void_p]
        lib.h3_last_error.restype = ctypes.c_char_p
        lib.h3_last_error.argtypes = [ctypes.c_void_p]
        lib.h3_device.restype = ctypes.POINTER(H3DeviceInfo)
        lib.h3_device.argtypes = [ctypes.c_void_p]
        lib.h3_model.restype = ctypes.POINTER(H3ModelInfo)
        lib.h3_model.argtypes = [ctypes.c_void_p]
        lib.h3_cache_set_enabled.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.h3_cache_clear.argtypes = [ctypes.c_void_p]
        lib.h3_cache_get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(H3CacheInfo)]
        lib.h3_generate.restype = ctypes.POINTER(H3Result)
        lib.h3_generate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(H3Params)]
        lib.h3_result_free.argtypes = [ctypes.POINTER(H3Result)]

        self._ctx = lib.h3_load_dir(self.model_dir.encode())
        if not self._ctx:
            raise H3Error(f"h3_load_dir failed: {self._last_error()}")
        lib.h3_cache_set_enabled(self._ctx, 1)
        return self

    def close(self):
        if self._lib and self._ctx:
            self._lib.h3_free(self._ctx)
        self._ctx = None
        if getattr(self, "_old_cwd", None):
            os.chdir(self._old_cwd)

    # --- info ---

    def device(self) -> dict:
        d = self._lib.h3_device(self._ctx).contents
        return {
            "name": d.name.decode(),
            "architecture": d.architecture.decode(),
            "physical_memory": d.physical_memory,
            "metal4": d.metal4,
            "unified_memory": d.unified_memory,
        }

    def model(self) -> dict:
        m = self._lib.h3_model(self._ctx).contents

        def comp(c):
            return {"bytes": c.bytes, "files": c.files, "tensors": c.tensors}

        return {
            "text_encoder": comp(m.text_encoder),
            "fl2va_transformer": comp(m.fl2va_transformer),
            "ref2va_transformer": comp(m.ref2va_transformer),
            "video_vae": comp(m.video_vae),
            "audio_vae": comp(m.audio_vae),
        }

    def cache_info(self) -> dict:
        info = H3CacheInfo()
        self._lib.h3_cache_get_info(self._ctx, ctypes.byref(info))
        return {
            "embedding_entries": info.embedding_entries,
            "embedding_bytes": info.embedding_bytes,
            "prepared_dit": bool(info.prepared_dit),
            "video_decoder": bool(info.video_decoder),
        }

    def clear_cache(self):
        self._lib.h3_cache_clear(self._ctx)

    # --- generation ---

    def generate(
        self,
        prompt: str,
        *,
        output_path: str,
        refs: list[dict] | None = None,
        on_progress=None,
        **overrides,
    ) -> dict:
        """Generate a video. refs: [{"kind": "image|video|audio|video_audio",
        "path": ..., "audio_path"?: ..., "include_embedded_audio"?: bool}]"""
        params = _default_params()
        params.update(overrides)

        self.progress_callback = on_progress
        p = H3Params()  # ctypes zero-initializes: ints=0, pointers=NULL
        # char* fields need bytes; encode str here so every caller can pass
        # plain paths (root-cause guard, not per-caller)
        charp = ("output_path", "first_frame", "last_frame")
        for key, value in params.items():
            if key in charp and isinstance(value, str):
                value = value.encode()
            setattr(p, key, value)
        p.output_path = output_path.encode()

        # References: build an array kept alive until h3_generate returns.
        self._refs.clear()
        arr = (H3Reference * len(refs or []))()
        for i, r in enumerate(refs or []):
            kind = r["kind"]
            if isinstance(kind, str):
                kind = {
                    "image": H3_REFERENCE_IMAGE,
                    "video": H3_REFERENCE_VIDEO,
                    "audio": H3_REFERENCE_AUDIO,
                    "video_audio": H3_REFERENCE_VIDEO_AUDIO,
                }[kind]
            ref = H3Reference(
                kind=kind,
                path=r["path"].encode() if r.get("path") else None,
                audio_path=r["audio_path"].encode() if r.get("audio_path") else None,
                include_embedded_audio=int(r.get("include_embedded_audio", False)),
            )
            arr[i] = ref
        self._refs.append(arr)
        p.references = arr if len(arr) else None
        p.reference_count = len(arr)

        p.on_frame = self._cb_frame
        p.on_progress = self._cb_progress
        p.callback_opaque = None

        result = self._lib.h3_generate(self._ctx, prompt.encode(), ctypes.byref(p))
        if not result:
            raise H3Error(f"h3_generate failed: {self._last_error()}")
        meta = result.contents
        out = {
            "width": meta.width,
            "height": meta.height,
            "frames": meta.frames,
            "fps": meta.fps,
            "sample_rate": meta.sample_rate,
            "seed": meta.seed,
        }
        self._lib.h3_result_free(result)
        return out

    # --- internals ---

    def _last_error(self) -> str:
        err = self._lib.h3_last_error(self._ctx)
        return err.decode() if err else "unknown error"

    def _on_frame(self, frame_ptr, opaque):
        if self.frame_callback:
            self.frame_callback(frame_ptr.contents)
        return 0

    def _on_progress(self, phase, completed, total, opaque):
        if self.progress_callback:
            self.progress_callback(phase.decode() if phase else "", completed, total)
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _main():
    ap = argparse.ArgumentParser(description="h3.c generation via libh3.dylib")
    ap.add_argument("--lib", default=os.environ.get(
        "H3C_LIBRARY", "/Users/vincent/tool/h3.c/libh3.dylib"))
    ap.add_argument("--model-dir", default=os.environ.get(
        "H3C_MODEL_DIR", "/Users/vincent/tool/h3.c/MiniMax-H3"))
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--ref-image", action="append")
    ap.add_argument("--ref-audio", action="append")
    ap.add_argument("--ref-video", action="append")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--frames", type=int)
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--denoise-reuse", type=int)
    ap.add_argument("--dit-layers", type=int)
    ap.add_argument("--ssd-streaming", action="store_true")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--out", default="output.mp4")
    ap.add_argument("--info", action="store_true")
    args = ap.parse_args()

    refs = []
    for path in args.ref_image or []:
        refs.append({"kind": "image", "path": path})
    for path in args.ref_audio or []:
        refs.append({"kind": "audio", "path": path})
    for path in args.ref_video or []:
        refs.append({"kind": "video", "path": path})

    overrides = {}
    if args.width:
        overrides["width"] = args.width
    if args.height:
        overrides["height"] = args.height
    if args.frames:
        overrides["frames"] = args.frames
    if args.seconds:
        overrides["frames"] = max(1, round(args.seconds * 24))
    if args.steps:
        overrides["steps"] = args.steps
    if args.denoise_reuse:
        overrides["denoise_reuse"] = args.denoise_reuse
    if args.dit_layers:
        overrides["dit_layers"] = args.dit_layers
    if args.ssd_streaming:
        overrides["ssd_streaming"] = 1
    if args.seed:
        overrides["seed"] = args.seed

    with H3Engine(args.lib, args.model_dir) as engine:
        engine.load()
        if args.info:
            print("device:", engine.device())
            print("model:", engine.model())
            return
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        meta = engine.generate(
            args.prompt,
            output_path=args.out,
            refs=refs,
            on_progress=lambda phase, done, total: print(
                f"\r[{phase}] {done}/{total}", end="", flush=True),
            **overrides,
        )
        print(f"\nOK -> {args.out}  {meta}")


if __name__ == "__main__":
    _main()
