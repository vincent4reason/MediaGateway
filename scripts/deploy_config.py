"""Generate the launchd plist for the Gateway on 127.0.0.1:8600.

Usage:
  .venv/bin/python scripts/deploy_config.py                 # plist to stdout
  .venv/bin/python scripts/deploy_config.py -o <path>       # write to file
"""
import argparse
import os
import plistlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = "com.aifilm.gateway"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def build() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            os.path.join(ROOT, ".venv", "bin", "uvicorn"),
            "server.main:app",
            "--host", "127.0.0.1",
            "--port", "8600",
        ],
        "WorkingDirectory": ROOT,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "MG_BUDGET_GB": "36",
            "MG_DB": os.path.join(ROOT, "data", "gateway.db"),
            "MG_ASSETS": os.path.join(ROOT, "assets"),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        "StandardOutPath": os.path.join(ROOT, "data", "gateway.log"),
        "StandardErrorPath": os.path.join(ROOT, "data", "gateway.err.log"),
    }


def write(path: str = PLIST_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(build(), f)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=None,
                    help=f"output path (default stdout; deploy uses {PLIST_PATH})")
    args = ap.parse_args()
    if args.output:
        print(f"written: {write(args.output)}")
    else:
        plistlib.dump(build(), sys.stdout.buffer)
