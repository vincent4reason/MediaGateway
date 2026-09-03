"""Voice worker: TTS via CosyVoice microservice (127.0.0.1:8001).

The torch model lives in its own process (scripts/start_tts.sh, cosyvoice
.venv) — this worker only does HTTP through vendor/cosyvoice/client.py.
"""
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent.parent / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from cosyvoice import client as _client  # noqa: E402

TYPE = "voice"
MEM_GB = 8.7  # TTS 常驻独立 :8001 进程(torchaudio+模型); 此值供调度预算参考


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    text = (params.get("text") or "").strip()
    if not text:
        raise ValueError("text 必填")
    if not (params.get("voice") or (params.get("prompt_wav") and params.get("prompt_text"))):
        raise ValueError("需要 voice (voices.json voiceId) 或 prompt_wav+prompt_text")
    if cancel():  # TTS 短任务, 提交前检查一次即可
        raise RuntimeError("cancelled before start")

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
