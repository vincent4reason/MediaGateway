#!/usr/bin/env python3
"""P0 benchmark matrix for h3.c on M5 Pro (docs/h3_speed_plan.md §3).

Drives the live Gateway (:8600) serially with fixed prompt/seed/geometry,
records wall time per config, then SSIM (ffmpeg) of each output vs reference R.
Results: /tmp/h3bench/results.json + printed table. Stdlib only.
"""
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import os
GW = os.environ.get("BENCH_GW", "http://127.0.0.1:8602")  # 专用实例，免疫 launchd 重启
PROMPT = ("A young woman standing alone on a rainy Tokyo street at night, "
          "neon lights reflecting on wet pavement, cinematic")
BASE = {"prompt": PROMPT, "width": 864, "height": 480, "seconds": 5, "seed": 42}

CONFIGS = [
    ("R", dict(steps=20, dit_layers=50, denoise_reuse=1)),
    ("A", dict(steps=20, dit_layers=45, denoise_reuse=2)),
    ("B", dict(steps=20, dit_layers=45, denoise_reuse=2, token_reduction=True)),
    ("C", dict(steps=20, dit_layers=45, denoise_reuse=2, token_reduction=True,
               use_int8_row_fc2=True)),
    ("D", dict(steps=20, dit_layers=45, core_reuse=4, token_reduction=True)),
    ("E", dict(steps=6, dit_layers=45, denoise_reuse=1)),
    ("F", dict(steps=6, dit_layers=45, denoise_reuse=1,
               render_width=576, render_height=320)),
    ("G", dict(steps=6, dit_layers=45, denoise_reuse=1,
               render_width=576, render_height=320,
               token_reduction=True, use_int8_row_fc2=True)),
]
OUT = Path("/tmp/h3bench")


def api(method, path, body=None):
    req = urllib.request.Request(GW + path, method=method,
                                 **({"data": json.dumps(body).encode(),
                                     "headers": {"Content-Type": "application/json"}}
                                    if body is not None else {}))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def run_one(name, cfg):
    job = api("POST", "/v1/jobs", {"type": "video", "params": {**BASE, **cfg}})
    jid = job["id"]
    t0 = time.time()
    while True:
        if time.time() - t0 > 2400:
            raise SystemExit(f"{name} exceeded 2400s (job {jid} still {j['status']})")
        j = api("GET", f"/v1/jobs/{jid}")
        if j["status"] in ("completed", "failed", "cancelled"):
            break
        print(f"  [{name}] {j['status']} {round(j['progress'], 2)} {j['phase'] or ''}",
              flush=True)
        time.sleep(10)
    wall = time.time() - t0
    if j["status"] != "completed":
        raise SystemExit(f"{name} FAILED: {j['error']}")
    print(f"  [{name}] done in {wall:.0f}s -> {j['output_path']}", flush=True)
    return {"name": name, "job_id": jid, "wall_s": round(wall, 1),
            "gen_s": round((j["finished_at"] - j["started_at"]), 1),
            "output_path": j["output_path"], "config": cfg}


def ssim(a, b):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", a, "-i", b,
                        "-lavfi", "ssim", "-f", "null", "-"],
                       capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "SSIM" in line and "All:" in line:
            return float(line.split("All:")[1].split(" ")[0])
    return None


def main():
    OUT.mkdir(exist_ok=True)
    only = sys.argv[1:] or [c[0] for c in CONFIGS]
    results_path = OUT / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    def fp(name, cfg):
        payload = json.dumps({**BASE, **cfg}, sort_keys=True)
        return f"{name}:{hashlib.sha256(payload.encode()).hexdigest()[:8]}"

    done = {r.get("fingerprint") for r in results}
    for name, cfg in CONFIGS:
        if name not in only or fp(name, cfg) in done:
            continue
        print(f"=== {name}: {cfg}", flush=True)
        try:
            entry = run_one(name, cfg)
        except (SystemExit, OSError, KeyError, ValueError) as e:
            print(f"  [skip] {type(e).__name__}: {e}", flush=True)
            continue
        entry["fingerprint"] = fp(name, cfg)
        results.append(entry)
        results_path.write_text(json.dumps(results, indent=1))

    ref = next((r for r in results if r["name"] == "R" and r.get("output_path")), None)
    for r in results:
        r["ssim_vs_R"] = (ssim(ref["output_path"], r["output_path"])
                          if ref and r is not ref and r.get("output_path") else None)
        print(f"{r['name']}: wall={r['wall_s']}s ssim={r['ssim_vs_R']}", flush=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=1))
    print("DONE -> /tmp/h3bench/results.json", flush=True)


if __name__ == "__main__":
    main()
