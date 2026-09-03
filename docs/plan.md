# AI Media Gateway — 开发/部署计划

> 依据 `AI-Film-OS-M5-Pro-48GB-v2.md`，结合 P0 盘点（`docs/tools.md`）修订。
> 2026-09-04

## 架构修订（相对原文档）

1. **Asset Store 归影策** — 影策已有 asset store，Gateway 不再自建资产库。
   Gateway 只负责：接收任务 → 生成 → 把产物写到约定目录 → 返回文件路径。
   资产元数据（§16/§17）与 parent_assets 追踪链全部由影策管理。
2. **Gateway 全 Python FastAPI**（已确认）— cosyvoice/ACE-Step 必须 Python，
   iris/h3 的 ctypes bridge 已验证，Go cgo 是纯返工。
3. **不引入 Redis / PostgreSQL / Docker** — 单机 launchd + SQLite（仅 job 状态）+ 文件系统。
4. **Voice = cosyvoice**（非文档的 Qwen3-TTS MLX）；qwen LLM 与 qwen-asr 不在计划内。
5. 3D 导演台（§11）不在 Gateway 范围，归影策。

## Gateway 职责（收窄后）

- 统一 Job API：`POST /v1/{image,voice,music,video}/generate`、`GET /v1/jobs/:id`、cancel
- 内存预算调度：每引擎配置 `mem_gb` 估计值，运行中任务之和 + 新任务 ≤ 40GB（留系统余量），超出排队。h3 填大数（~35GB）即天然独占；iris 10.8 / cosyvoice 8.7 / ACE-Step 待 Phase 4 实测
- Worker 生命周期：常驻进程 + 懒加载 + 显式卸载
- 日志 / 重试 / 超时

## 阶段计划

> 进度：P0–P5 全部完成并实机验收（2026-09-04）。P6 未开始。

### Phase 1 — Gateway 骨架 ✅
- FastAPI：jobs 表（SQLite）+ 内存队列 + 内存预算调度
- **监听 :8600（接管旧 h3cweb 端口，有调用方）**：Phase 2 起必须兼容 h3cweb 现有 API 面
  （`/health` `/info` `/jobs` `/jobs/{id}` `/v1/videos*` `/files/{name}`），旧调用方无感迁移
- 从 h3cweb 复制 `h3_bridge.py`、`cosyvoice/client.py`、`voices.json`、`render/*.sh`
- 产物写 `assets/{job_id}/`，job 记录返回路径；不建资产库（Phase 6 对接影策 store）
- 验收：curl 提交任务 → 轮询状态 → 拿到产物路径

### Phase 2 — Video Worker ✅
- h3_bridge 接入，`/v1/video/generate`：prompt + refs + first/last frame → mp4
- 超时 / kill / 重试 1 次；与 ：8600 旧 server 二选一（迁到 Gateway 后停旧进程）

### Phase 3 — Voice Worker ✅
- tts_server(:8001) 或直调，`/v1/voice/generate`：character voice_id + text → wav

### Phase 4 — Music Worker（已定：独立部署）
- clone `ace-step/ACE-Step` 到 `~/tool/ace-step`，下载 HF 原版权重（不走 ComfyUI）
- **权重下载安排在 Phase 4 开始时**（已确认不提前）
- 薄 wrapper（常驻子进程或 CLI）→ wav；实测峰值内存填入调度配置 `mem_gb`
- 验收：`/v1/music/generate`（emotion/genre/duration）→ 45s BGM wav

### Phase 5 — Image Worker ✅
- iris.c（先 CLI 后 FFI），`/v1/image/generate` → png
- 打通角色一致性链：reference → 分镜图 → first/last frame

### Phase 6 — 影策对接 + Agent
- 组合任务 `POST /v1/shots/:id/render`（image→video→voice→music→ffmpeg）
- 影策经 REST 提交任务、按路径取产物、登记资产
- MCP server 暴露工具给 Claude Code / Codex

## 部署形态

```
launchd: com.aifilm.gateway （单 Python 进程 :8080，Worker 为其子进程/线程）
```

无容器、无 Redis、无 PostgreSQL。

## 风险

| 风险 | 对策 |
|---|---|
| h3 独占 48GB 内存 | Phase 2 起用互斥锁，实测后再细化优先级 |
| 与 ：8600 旧 server 并存冲突 | Phase 2 迁移完成后 launchd 停旧进程 |
| ACE-Step 未知 | Phase 4 先最小验证再接入 |
