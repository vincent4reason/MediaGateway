"""No-op worker: Phase 1 acceptance test — writes hello.txt, no GPU."""
from pathlib import Path

TYPE = "noop"
MEM_GB = 0.1


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    text = params.get("text", "hello gateway")
    out = job_dir / "hello.txt"
    out.write_text(text)
    return {"output_path": str(out), "bytes": out.stat().st_size}
