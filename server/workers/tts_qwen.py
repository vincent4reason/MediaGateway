"""TTS worker: Qwen3-TTS via mlx-audio CLI (Metal GPU), second voice engine.

Model: mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16 (predefined speakers,
emotion instruct). Voice cloning needs the Base variant + ref_audio/ref_text.
Env overrides: MLXAUDIO_DIR, MLXAUDIO_PYTHON (venv python), MLXAUDIO_BIN
(full command override, used by tests), MLXAUDIO_MODEL.
"""
from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

TYPE = "tts_qwen"
MEM_GB = 9.0  # measured peak RSS of one generation (~8.6GB, bf16 1.7B + tokenizer)

DEFAULT_DIR = os.environ.get("MLXAUDIO_DIR", "/Users/vincent/tool/mlx-audio")
DEFAULT_PYTHON = os.path.join(DEFAULT_DIR, ".venv/bin/python")
DEFAULT_MODEL = os.environ.get(
    "MLXAUDIO_MODEL", "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16")
DEFAULT_TIMEOUT = 600.0
# CustomVoice models require a speaker; plugin coalesces voice->model key, so
# callers often send none. Options: serena vivian uncle_fu ryan aiden ono_anna
# sohee eric dylan (model.get_supported_speakers()).
DEFAULT_VOICE = "serena"


def _wav_rate(path: Path):
    """Sample rate from a canonical PCM WAV header (no wave/soundfile dep)."""
    try:
        with open(path, "rb") as f:
            head = f.read(28)
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return struct.unpack("<I", head[24:28])[0]
    except OSError:
        pass
    return None


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    text = (params.get("text") or "").strip()
    if not text:
        raise ValueError("params.text is required")
    timeout = float(params.get("timeout", DEFAULT_TIMEOUT))

    args = [
        "--model", os.environ.get("MLXAUDIO_MODEL", DEFAULT_MODEL),
        "--text", text,
        "--output_path", str(job_dir),
        "--file_prefix", "output",
        "--join_audio",  # long text is segmented; join into a single output.wav
    ]
    voice = params.get("voice") or params.get("speaker") or DEFAULT_VOICE
    args += ["--voice", str(voice)]
    if params.get("speed") is not None:
        args += ["--speed", str(params["speed"])]
    if params.get("instruct"):
        args += ["--instruct", str(params["instruct"])]
    if params.get("ref_audio"):
        if not Path(params["ref_audio"]).is_file():
            raise ValueError(f"ref_audio not found: {params['ref_audio']}")
        args += ["--ref_audio", str(params["ref_audio"])]
    if params.get("ref_text"):
        args += ["--ref_text", str(params["ref_text"])]
    if params.get("lang_code"):
        args += ["--lang_code", str(params["lang_code"])]

    bin_override = os.environ.get("MLXAUDIO_BIN")
    cmd = ([bin_override] if bin_override
           else [os.environ.get("MLXAUDIO_PYTHON", DEFAULT_PYTHON),
                 "-m", "mlx_audio.tts.generate"]) + args

    if cancel():  # short task; subprocess cannot be interrupted mid-run
        raise Exception("cancelled")

    progress(0.1, "loading")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Exception(f"tts_qwen timeout after {timeout}s (process killed)")
    progress(0.95, "saving")

    outs = sorted(job_dir.glob("output*.wav"),
                  key=lambda f: (f.name != "output.wav", len(f.name), f.name))
    if proc.returncode != 0 or not outs:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise Exception(f"mlx-audio exited {proc.returncode}: {tail}")
    out = outs[0]
    return {"output_path": str(out), "sample_rate": _wav_rate(out),
            "model": cmd[cmd.index("--model") + 1]}
