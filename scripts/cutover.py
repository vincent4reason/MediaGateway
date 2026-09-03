"""Cutover: take :8600 from the frozen h3cweb server to the Gateway (launchd).

Idempotent. Run AFTER confirming no running jobs on the old server:
  .venv/bin/python scripts/cutover.py --dry-run   # checks only, changes nothing
  .venv/bin/python scripts/cutover.py             # execute
  .venv/bin/python scripts/cutover.py --yes       # skip the interactive confirm
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.deploy_config import build, write, LABEL, PLIST_PATH  # noqa: E402

PORT = 8600
OLD_DIR = "/Users/vincent/code/h3cweb"


def http(path, timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


def port_pids():
    out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}"],
                         capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def pids_cmdline(pids):
    for pid in pids:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True)
        yield pid, out.stdout.strip()


def confirm(msg, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def preflight(yes):
    """Return (ok, note). Old server must be present but idle before the kill."""
    status, body = http("/health")
    if status is None:
        print(":8600 has no listener — nothing to stop; will just bootstrap launchd.")
        return True, "no-listener"
    print(f":8600 alive (health={status}), processes: "
          f"{[c for _, c in pids_cmdline(port_pids())]}")
    # old server has no GET /jobs list endpoint (405 => it's the old h3cweb face);
    # we cannot enumerate its jobs, so require a human confirmation unless --yes.
    status, _ = http("/jobs")
    if status == 405:
        if not confirm("Old h3cweb server exposes no job list — are there NO running "
                       "jobs on it right now?", yes):
            print("Aborted: confirm the old server is idle, then re-run (or pass --yes).")
            return False, "unconfirmed"
    elif status == 200:
        print(f"GET /jobs list: {body[:400]!r} — verify nothing is running.")
        if not confirm("Does the list above show no running jobs?", yes):
            print("Aborted.")
            return False, "busy"
    return True, "ok"


def kill_old():
    pids = [pid for pid, cmd in pids_cmdline(port_pids())]
    if not pids:
        return
    subprocess.run(["kill", *map(str, pids)])
    for _ in range(50):  # 5s graceful, then SIGKILL
        if not port_pids():
            print(f"stopped old :8600 process(es) {pids}")
            return
        time.sleep(0.1)
    subprocess.run(["kill", "-9", *map(str, [p for p in port_pids()])])
    time.sleep(0.5)
    print(f"SIGKILLed lingering :8600 process(es) {pids}")


def bootstrap():
    path = write(PLIST_PATH)
    print(f"plist written: {path}")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                   capture_output=True)  # idempotent: ignore "not bootstrapped"
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        r2 = subprocess.run(["launchctl", "load", path], capture_output=True, text=True)
        if r2.returncode != 0:
            sys.exit(f"launchctl failed: bootstrap={r.stderr.strip()} load={r2.stderr.strip()}")
        print("loaded via launchctl load (bootstrap unavailable)")
    else:
        print(f"bootstrapped gui/{uid}/{LABEL}")


def wait_health(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        status, body = http("/health")
        if status == 200 and b"ok" in body:
            print(f"gateway healthy after {timeout - (end - time.time()):.1f}s: {body.decode()}")
            return True
        time.sleep(0.5)
    print("launchctl status for debugging:")
    subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)
    subprocess.run(["tail", "-20", os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), "data", "gateway.err.log")])
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="checks only, changes nothing")
    ap.add_argument("--yes", action="store_true", help="skip interactive confirm")
    args = ap.parse_args()

    ok, _ = preflight(args.yes)
    if not ok:
        return 1
    if args.dry_run:
        print(f"dry-run: would kill {port_pids() or 'nothing'} on :8600, write "
              f"{PLIST_PATH}, bootstrap gui/{os.getuid()}/{LABEL}, then poll /health.")
        print("planned plist:")
        import plistlib
        plistlib.dump(build(), sys.stdout.buffer)
        return 0

    kill_old()
    bootstrap()
    if not wait_health():
        return 1
    status, body = http("/info", timeout=5)
    print(f"\n/info ({status}):\n{body.decode()}")
    print("\nCutover complete. Old server is stopped; launchd keeps the Gateway alive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
