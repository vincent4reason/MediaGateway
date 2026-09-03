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
6. **h3cweb 冻结**（已确认）— 全面迁 Gateway：旧 projects 数据保留只读，
   旧 server 于部署切换时停止，新能力只进 MediaGateway。
7. **MCP 只在影策侧**（已确认）— 按文档 §27 分层 Agent→MCP→影策→Gateway，
   Gateway 只出 REST，不做自己的 MCP。
8. **调度行为（已定）**— 严格 FIFO 队头阻塞，video 失败不重试即 failed；
   Worker 用完即关（keep_loaded 可选常驻），仓库禁止 *.sh。

## Gateway 职责（收窄后）

- 统一 Job API：`POST /v1/{image,voice,music,video}/generate`、`GET /v1/jobs/:id`、cancel
- 内存预算调度：每引擎配置 `mem_gb` 估计值，运行中任务之和 + 新任务 ≤ 40GB（留系统余量），超出排队。h3 填大数（~35GB）即天然独占；iris 10.8 / cosyvoice 8.7 / ACE-Step 待 Phase 4 实测
- Worker 生命周期：常驻进程 + 懒加载 + 显式卸载
- 日志 / 重试 / 超时

## 阶段计划

> 进度：**全部完成**（2026-09-04）。P6 组合任务实机验收通过：
> 一个请求 → image+video(首帧自动接线)+voice+music+混音 → shot.mp4（约 2 分钟）。
> 剩余：影策侧经 MCP/REST 实际对接（验收标准「一句话→完整 shot」的 Agent 侧）。

### Phase 1 — Gateway 骨架 ✅
- FastAPI：jobs 表（SQLite）+ 内存队列 + 内存预算调度
- **监听 :8600（接管旧 h3cweb 端口，有调用方）**：Phase 2 起必须兼容 h3cweb 现有 API 面
  （`/health` `/info` `/jobs` `/jobs/{id}` `/v1/videos*` `/files/{name}`），旧调用方无感迁移
- 从 h3cweb 复制 `h3_bridge.py`、`cosyvoice/client.py`、`voices.json`、`render/*.sh`
- 产物写 `assets/{job_id}/`，job 记录返回路径；不建资产库（Phase 6 对接影策 store）
- 验收：curl 提交任务 → 轮询状态 → 拿到产物路径

### Phase 2 — Video Worker ✅
- h3_bridge 接入，`/v1/video/generate`：prompt + refs + first/last frame → mp4
- 超时 / kill；**失败不重试即 failed**（已确认，行为可预测优先）

### Phase 3 — Voice Worker ✅
- tts_server(:8001) 或直调，`/v1/voice/generate`：character voice_id + text → wav
- 生命周期（已实现）：Gateway 按需拉起 tts_server，空闲 300s 自动退出；外部启动的 server 只用不杀

### Phase 4 — Music Worker ✅
- `~/tool/ace-step`：官方 repo + `ACE-Step/Ace-Step1.5` 权重 9.4GB，MPS 正常
- 实测：45s BGM 暖 17s，峰值 RSS 14.8GB → `MEM_GB=15`；输出 48kHz WAV
- LM/thinking 歌词路径未启用（需要时再开）

### Phase 5 — Image Worker ✅
- iris.c（先 CLI 后 FFI），`/v1/image/generate` → png
- 打通角色一致性链：reference → 分镜图 → first/last frame

### Phase 6 — 影策对接 + Agent
- **前置：shell 脚本 Python 化（硬规则：仓库内禁止 *.sh，一切用 Python）**：
  - `vendor/render/{concat,mux,freeze}.sh` → `server/render.py`（ffmpeg 子进程封装，供组合任务调用），完成后删除全部 .sh
  - `scripts/start_tts.sh` 已随 voice 生命周期改造删除（Gateway 按需拉起/空闲退出）
  - 后续任何新脚本一律 Python，不允许再引入 .sh
- 组合任务 `POST /v1/shots/:id/render`（image→video→voice→music→ffmpeg）
- 影策经 REST 提交任务、按路径取产物、登记资产
- **Gateway 不做 MCP**（已确认）— MCP 归影策（§27：Agent→MCP→影策→Gateway）
- **验收标准（已确认）：自然语言一句话 → 完整 shot**（图+视频+配音+BGM+混音成片），
  链路经影策 Agent 驱动 Gateway 完成

## 部署形态

```
launchd: com.aifilm.gateway （单 Python 进程 **127.0.0.1:8600**，MG_BUDGET_GB=36，
Worker 为其子进程/线程；预算 36 + tts_server 空闲常驻 8.7 ≈ 物理上限 45GB，留 3GB 系统余量）
```

无容器、无 Redis、无 PostgreSQL。

## 风险

| 风险 | 对策 |
|---|---|
| h3 独占 48GB 内存 | Phase 2 起用互斥锁，实测后再细化优先级 |
| 与 ：8600 旧 server 并存冲突 | 部署切换时：确认无运行中任务 → 停旧 h3cweb server（已冻结，projects 数据只读保留）→ launchd 起 Gateway |
| ACE-Step 未知 | Phase 4 先最小验证再接入 |

## API 契约速查（影策对接必读）

同一引擎多入口的响应字段映射（GET 单个 job）：

| 入口 | id 字段 | 状态词表 | 独有字段 |
|---|---|---|---|
| `GET /v1/jobs/{id}` | `id` | queued/running/completed/failed/cancelled | params、meta、progress、phase |
| `GET /jobs/{id}`（h3cweb 兼容） | `job_id` | 同上；**cancelled 对旧调用方显示 failed** | phase、progress |
| `GET /v1/videos/{id}`（Sora 风格） | `id` | queued/**in_progress**/completed/failed | progress |

提交入口与产物：`POST /v1/jobs` 与 `POST /jobs` 响应结构不同（整行 vs {job_id,status,output_path}）；
新产物统一在 `assets/{job_id}/`，经 `/v1/videos/{id}/content` 或直接路径取，`/files/{name}` 只服务 h3cweb 历史文件。

## 已知限制（审查结论，接受现状）

1. **video job 无超时中断** — h3 引擎在进程内，挂起时无法安全强杀；需 `launchctl kickstart -k gui/501/com.aifilm.gateway` 重启（启动时自动把遗留 running job 标记 failed）
2. **cancel 是协作语义** — 已进入不可中断段（生成中）的任务可能 cancel 后仍 completed；以最终 status 为准
3. **freeze 定格尾段静音** — 音轨原样保留不延长；需要卡点带声先 mux 再 freeze
4. **参数命名**：video 用 `seconds`，music 用 `duration_s`（历史约定，meta 字段名见各 worker 返回）
