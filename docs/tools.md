# 本地引擎盘点（P0 实测）

> 2026-09-04 · Mac M5 Pro 48GB · Gateway Worker 实现依据。
> 工具根目录：`/Users/vincent/tool`（后续所有工具项目放这里）。

## 内存预算总览（48GB 统一内存）

| 引擎 | 峰值内存（实测） | 并行结论 |
|---|---|---|
| iris.c (flux-klein-4b) | 10.8GB RSS | 可与 cosyvoice 并行 |
| cosyvoice 0.5B | 8.7GB RSS | 可与 iris 并行 |
| h3.c (MiniMax-H3) | 未实测（server 独占调度） | 单独跑，其余排队 |
| 三者串行总量 | — | 安全；两个 DiT 不同时跑 |

## 1. iris.c — Image Engine

- 位置：`~/tool/iris.c`，二进制 `./iris`，另有 `libiris.dylib`（后续可 ctypes，同 h3 模式）
- 权重：`flux-klein-4b/` 15G（主力）、`zimage-turbo/` 31G（备用）
- CLI：

```bash
cd ~/tool/iris.c
./iris -d flux-klein-4b -p "PROMPT" --seed 42 --steps 4 -W 1024 -H 1024 -o out.png
# img2img: 加 -i input.png
```

- 实测（1024×1024 / 4 steps）：**66.8s 总耗时（含模型加载）**，10.8GB RSS
- 已验证能力：txt2img、img2img（`run_test.py` 内置参考图回归）

## 2. h3.c — Video Engine

- 位置：`~/tool/h3.c`，`libh3.dylib`；权重 `MiniMax-H3/` 196G（`FL2VA/` 首尾帧、`Ref2VA/` 参考视频 两种 transformer）
- 调用：**ctypes FFI 已验证** — 复用 `/Users/vincent/code/h3cweb/server/h3_bridge.py`（`H3Engine` 类，完整 h3_params 透传，on_progress 回调）
- 关键约束：
  - 加载时必须 chdir 到 dylib 所在目录（shaders.metal 相对路径）
  - 分辨率必须 32 的倍数，上限 768×1344
  - 默认 864×480@24fps，默认 56 帧 / 20 steps（h3cweb 实际用 6 steps + denoise_reuse）
- 常驻 server 已在运行：`uvicorn server.main:app` **:8600**（h3cweb，FastAPI + FIFO 队列 + OpenAI /v1/videos 兼容层）
- 实测：未单独跑（用户已验证 FFI；后续 Worker 直接复用 bridge）

## 3. cosyvoice — Voice Engine

- 位置：`~/tool/cosyvoice`（Fun-CosyVoice3-0.5B），Python 环境 `.venv/bin/python`
- 两种用法：
  1. 微服务：`tts_server.py`（FastAPI **:8001**，当前未启动）
     `POST /tts {text, prompt_text, prompt_wav, speed, out_path}` → `{ok, wav_b64|path, sample_rate}`
     `prompt_text` 必须含 `<|endofprompt|>`
  2. 直调：`.venv/bin/python test_tts.py`（zero_shot 推理）
- 音色克隆：zero-shot，每个 voiceId = 参考音频 + 参考文本；注册表复用 `h3cweb/workers/cosyvoice/voices.json`，客户端复用 `client.py`（重试逻辑齐全）
- 实测：冷启动约 4 分钟（含 modelscope 下载 wetext）；热加载 **4.2s** + 3 秒音频推理 **8.0s**，8.7GB RSS，采样率 24000

## 4. 其他

- **不列入开发计划**：`~/tool/qwen`（27B LLM）、`~/tool/qwen-asr` — 与本流水线无关
- Music：ACE-Step 1.5 **未部署**，Phase 4 下载
- FFmpeg：系统级，h3cweb `workers/render/{concat,mux,freeze}.sh` 可直接复用

## 5. h3cweb 可复制清单 → MediaGateway

| 文件 | 用途 |
|---|---|
| `server/h3_bridge.py` | h3 ctypes FFI 核心（原样复制） |
| `workers/cosyvoice/client.py` + `voices.json` | TTS 客户端 + 音色表（原样复制） |
| `workers/render/*.sh` | ffmpeg 拼接/混音/冻帧（原样复制） |
| `server/main.py` | 参考（队列/进度模式），MediaGateway 重写为多 Worker 统一调度 |
