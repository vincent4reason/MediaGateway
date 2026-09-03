"""Voice worker: TTS via CosyVoice microservice (127.0.0.1:8001).

The torch model lives in its own process (cosyvoice .venv) — this worker only
does HTTP through vendor/cosyvoice/client.py. Lifecycle is owned here:
spawn on first job, auto-exit after COSYVOICE_IDLE_EXIT_S (default 300s)
without jobs. An externally-started server is used but never killed.
Passing params.base_url skips lifecycle entirely (caller manages the server).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent.parent / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from cosyvoice import client as _client  # noqa: E402

TYPE = "voice"
MEM_GB = 8.7  # TTS 进程运行期间(torchaudio+模型); 供调度预算参考

TTS_DIR = os.environ.get("COSYVOICE_DIR", "/Users/vincent/tool/cosyvoice")
IDLE_EXIT_S = float(os.environ.get("COSYVOICE_IDLE_EXIT_S", "300"))

_proc: subprocess.Popen | None = None
_last_use = 0.0
_lock = threading.Lock()


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(
                f"{_client.DEFAULT_BASE_URL}/health", timeout=2) as r:
            return json.loads(r.read()).get("model_loaded") is True
    except Exception:  # noqa: BLE001 - not up yet == unhealthy
        return False


def _ensure_server(progress):
    global _proc, _last_use
    with _lock:
        _last_use = time.time()
        if _healthy():
            return
        if _proc is None or _proc.poll() is not None:
            log = open(os.path.join(TTS_DIR, "tts_server.log"), "ab")
            _proc = subprocess.Popen(
                [os.path.join(TTS_DIR, ".venv/bin/python"), "tts_server.py"],
                cwd=TTS_DIR, stdout=log, stderr=log)
        progress(0.05, "starting tts")
        deadline = time.time() + 180
        while time.time() < deadline:
            if _healthy():
                return
            time.sleep(1)
        raise RuntimeError("tts server model not loaded after 180s "
                           f"(log: {TTS_DIR}/tts_server.log)")


def _watchdog():
    # only ever kills a server we spawned ourselves
    global _proc
    while True:
        time.sleep(30)
        with _lock:
            if (_proc is not None and _proc.poll() is None
                    and time.time() - _last_use > IDLE_EXIT_S):
                _proc.terminate()
                _proc = None


threading.Thread(target=_watchdog, daemon=True).start()


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    text = (params.get("text") or "").strip()
    if not text:
        raise ValueError("text 必填")
    if not (params.get("voice") or (params.get("prompt_wav") and params.get("prompt_text"))):
        raise ValueError("需要 voice (voices.json voiceId) 或 prompt_wav+prompt_text")
    if cancel():  # TTS 短任务, 提交前检查一次即可
        raise RuntimeError("cancelled before start")

    if not params.get("base_url"):
        _ensure_server(progress)

    progress(0.1, "synthesizing")
    out = str(job_dir / "dialogue.wav")
    try:
        out, sample_rate = _client.synthesize(
            text,
            voice=params.get("voice"),
            out=out,
            prompt_wav=params.get("prompt_wav"),
            prompt_text=params.get("prompt_text"),
            speed=float(params.get("speed", 1.0)),
            base_url=params.get("base_url") or _client.DEFAULT_BASE_URL,
        )
    except SystemExit as e:  # client 用 SystemExit 报错; 调度器只捕 Exception
        raise RuntimeError(f"TTS failed: {e.code or e}") from None
    return {"output_path": out, "sample_rate": sample_rate}
