# AI Media Gateway

A local AI media generation gateway for Apple Silicon (built on a Mac M5 Pro 48GB).
It sits between a director frontend ([影策 / open-ai-canvas](https://github.com/ddcat-ai/open-ai-canvas))
and a set of local inference engines, exposing one unified async job API.

**中文文档:[README.zh-CN.md](README.zh-CN.md)**

```text
Director UI (影策 canvas)          POST /v1/jobs { type, params }
        │
        ▼
┌─────────────────────────────────────────────┐
│ AI Media Gateway  (FastAPI, :8600)          │
│  job queue · SQLite · memory-budget         │
│  scheduler · OpenAI-compatible faces        │
└──────────────┬──────────────────────────────┘
               │  worker contract (load → run → release)
   ┌───────────┼───────────┬────────────┬──────────────┐
   ▼           ▼           ▼            ▼              ▼
 image      video       voice      tts_qwen        music
 iris.c     h3.c      CosyVoice   Qwen3-TTS       ACE-Step
 (FLUX)   (MiniMax-H3, 0.5B        (mlx-audio)     1.5 (MLX)
           Metal, Ref2VA)                            + FFmpeg render
```

## Engines

| Worker | Engine | Output | Notes |
|---|---|---|---|
| `image` | iris.c — FLUX.2 Klein 4B (Metal) | PNG | txt2img / img2img, up to 16 reference images |
| `video` | [h3.c](https://github.com/antirez/h3.c) MiniMax-H3 (Metal) | MP4 | T2V/I2V/FL2VA/Ref2VA (audio-conditioned lip sync), profiles |
| `voice` | CosyVoice 0.5B (zero-shot voice clones) | WAV | voice registry `vendor/cosyvoice/voices.json` |
| `tts_qwen` | Qwen3-TTS 1.7B via [mlx-audio](https://github.com/Blaizzy/mlx-audio) | WAV | second voice engine |
| `music` | ACE-Step 1.5 | WAV | instrumental / lyrics |
| `shot` | composite: image → voice → video → music → mux | MP4 | draft / quality profiles, h3 audio muted, TTS original laid back |
| `concat` / `noop` | FFmpeg | MP4 | multi-shot stitch with music bed |

## Highlights

- **Unified job API** — `POST /v1/jobs` + `GET /v1/jobs/{id}` + cooperative cancel;
  everything lands in `assets/{job_id}/`.
- **OpenAI-compatible faces** for a director frontend:
  `POST /v1/videos` (Sora-style multipart, incl. cancel + reconciliation),
  `POST /v1/images/generations` and `/v1/images/edits`,
  `POST /v1/audio/speech`,
  `POST /v1/chat/completions` (local qwen3.8-27B MLX, memory-mutexed with video).
- **Memory-budget scheduler** — 40 GB budget; the 19 GB LLM and the 35 GB video
  engine are mutually exclusive and unload each other automatically.
- **Use-then-release lifecycle** — engines close after each job (`keep_loaded`
  opts out); TTS/LLM servers spawn on demand and idle-exit.
- **Benchmark-driven video profiles** (M5 Pro measured, 864×480 / 120 frames):

| Profile | Config | Wall time | vs reference |
|---|---|---:|---:|
| `reference` | 20 steps / 50 layers / reuse 1 | 1291 s | 1× |
| `quality` | 20 steps / 45 layers / core-reuse 4 / token reduction | **280 s** | **4.6×** |
| `standard` | 6 steps / 45 layers / reuse 1 | 340 s | 3.8× |
| `draft` | + internal canvas 576×320 | **110 s** | **11.7×** |

  INT8 FC2 measured **zero gain** on M5 Pro and is excluded from profiles.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" httpx python-multipart

# engines are expected under /Users/<you>/tool (see docs/tools.md);
# paths are overridable per worker via env vars (IRIS_BIN, H3C_LIBRARY, COSYVOICE_DIR, ...)

.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8600
```

Run tests (no GPU needed, engines are mocked):

```bash
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

Environment: `MG_DB`, `MG_ASSETS`, `MG_BUDGET_GB` (default 40), plus per-worker
overrides documented in `server/workers/*.py` module docstrings.

## Repo layout

```text
server/
  main.py            FastAPI app, generic job API
  core.py            scheduler (memory budget, priorities, cooperative cancel),
                     SQLite store, worker auto-discovery contract
  compat_h3cweb.py   Sora-style video face (newapi protocol)
  compat_openai.py   OpenAI image / audio / music faces
  compat_chat.py     OpenAI chat face → local MLX LLM
  render.py          FFmpeg wrappers: mux (voice+BGM, mute source), concat, freeze
  llm.py             local qwen MLX server lifecycle (spawn / idle-exit / unload)
  workers/           noop | image | video | voice | tts_qwen | music | shot | concat | mix
vendor/h3_bridge.py  ctypes FFI for libh3.dylib
scripts/             deploy_config.py (launchd plist) · cutover.py · h3_bench.py
docs/                plan.md · tools.md (engine inventory) · h3_speed_plan.md
```

## Related

- [影策 / open-ai-canvas](https://github.com/ddcat-ai/open-ai-canvas) — the director
  OS this gateway is wired into (see our fork `MediaGateway_YingCe`).
- [antirez/h3.c](https://github.com/antirez/h3.c) — the video engine and the
  ctypes bridge source.
