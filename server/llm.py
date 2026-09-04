"""LLMManager: on-demand lifecycle for the local qwen3.8-27B MLX server (mtplx).

Module singleton, same pattern as server/workers/voice.py: spawn on first
chat request, idle-exit after QWEN_IDLE_EXIT_S, `_busy` guard so the
watchdog never kills mid-generation. An already-running server on the port
is adopted (and CAN be killed on unload — unlike voice, 16GB vs video GPU
memory is a hard mutual exclusion, see core._admit_next).
"""
from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

QWEN_DIR = Path(os.environ.get("QWEN_DIR", "/Users/vincent/tool/qwen"))
PORT = int(os.environ.get("QWEN_PORT", "8000"))
BASE_URL = os.environ.get("QWEN_BASE_URL", f"http://127.0.0.1:{PORT}")
MEM_GB = float(os.environ.get("QWEN_MEM_GB", "19.3"))  # measured peak RSS
READY_TIMEOUT_S = float(os.environ.get("QWEN_READY_TIMEOUT_S", "300"))
IDLE_EXIT_S = float(os.environ.get("QWEN_IDLE_EXIT_S", "120"))

_DEFAULT_CMD = (
    "/opt/homebrew/bin/mtplx serve "
    "--model /Users/vincent/tool/qwen/models/Youssofal--Qwen3.8-27B-MTPLX-Optimized-Speed "
    "--paged-kv-quant q8 "
    f"--port {PORT}"
)
CMD = shlex.split(os.environ.get("QWEN_SERVE_CMD") or _DEFAULT_CMD)

_proc: subprocess.Popen | None = None
_last_use = 0.0
_busy = False           # a chat completion is in flight — never kill while set
_loading = False        # ensure() is spawning/waiting for readiness — counts as resident
_unload_pending = False  # core asked for memory back; fire at next non-busy tick
_lock = threading.RLock()
_known = False          # cached port-health, so core's 0.5s poll doesn't hammer /health
_checked = 0.0


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — not up yet == unhealthy
        return False


def busy() -> bool:
    """True while a chat is in flight or the server is mid-spawn — the
    scheduler must wait instead of pulling memory out from under us."""
    with _lock:
        return _busy or _loading


def resident() -> bool:
    """True while LLM memory is occupied (our proc alive, mid-spawn, or
    something serves the port). Cached for 5s — safe for the scheduler's
    tight poll loop."""
    global _known, _checked
    with _lock:
        if _loading or (_proc is not None and _proc.poll() is None):
            return True
        if time.time() - _checked < 5:
            return _known
    _known = _healthy()
    _checked = time.time()
    return _known


def _port_pids() -> list[int]:
    try:
        out = subprocess.check_output(
            ["lsof", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
            timeout=5, stderr=subprocess.DEVNULL)
        return [int(p) for p in out.split()]
    except Exception:  # noqa: BLE001 — nothing listening / lsof missing
        return []


def ensure(timeout: float = READY_TIMEOUT_S):
    """Server ready for a chat request: adopt an external one or spawn ours."""
    global _last_use, _proc, _unload_pending, _loading
    with _lock:
        _last_use = time.time()
        _unload_pending = False
    if _healthy():
        return
    with _lock:
        # counts as resident() so the scheduler won't admit a 35GB video job
        # while we're mid-spawn (TOCTOU: port isn't bound yet)
        _loading = True
        if _proc is None or _proc.poll() is not None:
            log = open(QWEN_DIR / "serve.log", "a")
            log.write(f"\n[llm.py] spawn {time.strftime('%F %T')} {' '.join(CMD)}\n")
            log.flush()
            _proc = subprocess.Popen(CMD, cwd=QWEN_DIR, stdout=log, stderr=log)
            log.close()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _healthy():
                return
            with _lock:
                proc = _proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"qwen serve exited code={proc.returncode} (log: {QWEN_DIR}/serve.log)")
            time.sleep(1)
        raise RuntimeError(f"qwen serve not ready after {timeout}s (log: {QWEN_DIR}/serve.log)")
    finally:
        with _lock:
            _loading = False


def unload() -> bool:
    """Stop the server and free its memory. Returns True once the port is dark."""
    global _proc, _unload_pending, _known, _checked
    with _lock:
        if _busy or _loading:
            # never kill mid-generation or mid-spawn; watchdog retries later
            _unload_pending = True
            return False
        _unload_pending = False
        proc, _proc = _proc, None
        _known, _checked = False, 0.0
    if proc is not None and proc.poll() is None:
        proc.terminate()  # wrapper may not reap its server child — port sweep below catches that
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=3)
    deadline = time.time() + 10
    while True:
        pids = _port_pids()
        if not _healthy() and not pids:
            return True
        if time.time() > deadline:
            return False
        # mtplx traps SIGTERM for a slow graceful shutdown — escalate to SIGKILL
        # and keep re-sending until the port is actually dark
        sig = signal.SIGKILL if time.time() > deadline - 5 else signal.SIGTERM
        for p in pids:
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.5)


def request_unload() -> None:
    """Called by the scheduler before admitting a video/shot job. If a chat is
    in flight, defer to the watchdog instead of killing mid-generation."""
    global _unload_pending
    with _lock:
        if _busy:
            _unload_pending = True
            return
    unload()


@contextlib.contextmanager
def busy_guard():
    global _busy, _last_use
    with _lock:
        _busy = True
    try:
        yield
    finally:
        with _lock:
            _busy = False
            _last_use = time.time()


def _watchdog():
    while True:
        time.sleep(5)
        with _lock:
            if _busy:
                continue
            due = _unload_pending or (
                _last_use and time.time() - _last_use > IDLE_EXIT_S)
        if due and resident():
            unload()


threading.Thread(target=_watchdog, daemon=True).start()
