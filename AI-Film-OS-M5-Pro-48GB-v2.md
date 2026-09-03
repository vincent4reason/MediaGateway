# AI Film OS --- M5 Pro 48GB 本地 AI 电影生产流水线

> **版本：v2 · Native Apple Silicon Runtime**
>
> 以 `影策 + AI Media Gateway` 为控制与调度核心，采用
> `iris.c / FLUX.2 Klein 4B`、`mlx-audio / Qwen3-TTS MLX`、`h3.c` 与
> `ACE-Step 1.5` 构成本地媒体生成层。

> **核心架构：影策 + AI Media Gateway + iris.c / FLUX.2 Klein 4B +
> mlx-audio / Qwen3-TTS MLX + ACE-Step 1.5 + h3.c + FFmpeg**

## 1. 项目定位

在 Mac M5 Pro 48GB 上构建本地 AI 短剧 / 电影生产系统。

核心原则：

-   **影策**：AI Director OS，负责导演、项目、剧本、角色、场景、分镜与
    3D 导演台。
-   **AI Media Gateway**：统一 API、任务队列、模型路由、资源调度和 Asset
    管理。
-   **Image
    Engine**：`iris.c + FLUX.2 Klein 4B`，负责角色、场景、分镜及参考图。
-   **Voice Engine**：`mlx-audio + Qwen3-TTS MLX`，负责角色对白与配音。
-   **ACE-Step 1.5**：负责 BGM / OST / 音乐。
-   **h3.c + Metal**：负责本地视频生成。
-   **FFmpeg**：负责最终后期与成片。

完整链路：

``` text
剧本
  ↓
角色
  ↓
场景
  ↓
分镜
  ↓
3D导演台
  ↓
角色/场景参考图
  ↓
生图
  ↓
h3.c 视频
  ↓
Qwen3-TTS MLX 配音
  ↓
ACE-Step 1.5 BGM
  ↓
FFmpeg 后期
  ↓
FINAL MOVIE
```

------------------------------------------------------------------------

# 2. 总体架构

``` text
                         Mac M5 Pro 48GB
                                │
                                ▼
                    ┌──────────────────────┐
                    │         影策          │
                    │    AI Director OS    │
                    │                      │
                    │ 剧本 / 角色 / 场景     │
                    │ 分镜 / 画布 / 资产     │
                    │ 3D导演台 / Agent      │
                    │ MCP / 项目管理         │
                    └──────────┬───────────┘
                               │
                               │ HTTP / REST
                               │ WebSocket
                               ▼
                ┌──────────────────────────────┐
                │      AI Media Gateway        │
                │                              │
                │ API / Job Queue / Scheduler  │
                │ Model Router / Asset Manager │
                │ Cache / Task Status          │
                └──────┬────────┬────────┬─────┘
                       │        │        │
              ┌────────▼──┐ ┌───▼────┐ ┌─▼────────┐
              │ Image      │ │ Voice  │ │ Video    │
              │ Worker     │ │ Worker │ │ Worker   │
              └────┬───────┘ └──┬─────┘ └────┬─────┘
                   │             │            │
                   ▼             ▼            ▼
              Qwen Image     Qwen3-TTS MLX    h3.c
              FLUX           Qwen3-TTS      Metal
                   │             │            │
                   ▼             ▼            ▼
                 Image         Voice        Video

                               ┌──────────────┐
                               │ Music Worker │
                               └──────┬───────┘
                                      ▼
                                ACE-Step 1.5
                                      │
                                      ▼
                                    BGM

                       ┌──────────────┴─────────────┐
                       ▼                            ▼
                    Voice                          BGM
                       │                            │
                       └────────────┬───────────────┘
                                    ▼
                              FFmpeg / Editor
                                    │
                                    ▼
                              FINAL MOVIE
```

------------------------------------------------------------------------

# 3. 系统分层

## 3.1 Control Plane：影策

影策作为整个系统的 **AI Director OS**。

负责：

-   剧本
-   角色
-   场景
-   分镜
-   Shot 管理
-   参考图片
-   3D 导演台
-   素材管理
-   Agent
-   MCP
-   项目管理
-   生成任务管理

影策主要回答：

``` text
What to create?
When to create?
Which character?
Which scene?
Which shot?
Which reference?
Which model?
```

影策不承担所有模型推理。

------------------------------------------------------------------------

# 4. AI Media Gateway

AI Media Gateway 是整个系统的核心中间层。

``` text
影策
 │
 ▼
AI Media Gateway
 │
 ├── Image Worker
 ├── Voice Worker
 ├── Music Worker
 └── Video Worker
```

## 4.1 Gateway 职责

### 统一 API

``` http
POST /v1/image/generate
POST /v1/voice/generate
POST /v1/music/generate
POST /v1/video/generate

GET  /v1/jobs/:id
POST /v1/jobs/:id/cancel

GET  /v1/assets/:id
POST /v1/assets
DELETE /v1/assets/:id
```

### 主要功能

-   Model Router
-   Job Queue
-   GPU / Memory Scheduler
-   Worker 管理
-   任务状态
-   Asset 管理
-   Cache
-   Retry
-   Timeout
-   日志
-   错误处理
-   API 统一化

------------------------------------------------------------------------

# 5. Job Queue 与资源调度

M5 Pro 48GB 是统一内存架构。

因此不建议多个大型模型同时占满内存。

``` text
                    Job Queue
                       │
            ┌──────────┼──────────┐
            │          │          │
            ▼          ▼          ▼
          IMAGE      VOICE      VIDEO
            │          │          │
            └──────────┼──────────┘
                       ▼
                    Scheduler
                       │
                       ▼
                 M5 Pro 48GB
```

大型任务推荐：

``` text
Video Generation
      ↓
释放资源
      ↓
Voice Generation
      ↓
释放资源
      ↓
Music Generation
```

轻量任务可以并行。

核心原则：

> **稳定完成整条流水线，比同时运行最多模型更重要。**

------------------------------------------------------------------------

# 6. Image Engine

Image Engine 负责：

-   角色设定图
-   角色三视图
-   面部特写
-   表情
-   服装
-   场景概念图
-   分镜图
-   首帧
-   尾帧
-   Reference Image

推荐结构：

``` text
Image Worker
    │
    └── iris.c
          │
          └── FLUX.2 Klein 4B
                │
                └── Metal / MPS
```

统一调用：

``` text
影策
 ↓
POST /v1/image/generate
 ↓
Image Worker
 ↓
Image Model
 ↓
PNG / JPG
 ↓
Asset Store
```

Image Worker 应与具体模型解耦，方便以后替换模型。

------------------------------------------------------------------------

# 7. Runtime Model Configuration

核心 Worker 固定为本地原生推理路径：

  Worker   Runtime        Model               Backend
  -------- -------------- ------------------- --------------------
  Image    `iris.c`       `FLUX.2 Klein 4B`   Metal / MPS
  Voice    `mlx-audio`    `Qwen3-TTS MLX`     MLX
  Music    ACE-Step 1.5   ACE-Step 1.5        Apple Silicon
  Video    `h3.c`         h3.c                Metal
  Post     FFmpeg         FFmpeg              CPU / VideoToolbox

推荐配置：

``` yaml
image:
  engine: iris.c
  model: flux-klein-4b
  backend: mps

voice:
  engine: mlx-audio
  model: mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16

video:
  engine: h3.c
  backend: metal

music:
  engine: ace-step
  model: ACE-Step 1.5

post:
  engine: ffmpeg
```

Voice Worker 可根据任务切换 Qwen3-TTS MLX 模型：

-   `1.7B-CustomVoice`：角色配音主力。
-   `1.7B-Base`：需要参考音频 / Voice Cloning 时使用。
-   `0.6B`：低延迟或资源紧张时使用。

## 8. Character System

角色是 AI Film OS 的核心资产。

每个角色拥有唯一：

``` text
character_id
```

示例：

``` yaml
character_id: character_001

name: 女主角

age: 24

appearance:
  hair: black
  eyes: brown
  body: slim

personality:
  - calm
  - intelligent
  - mysterious

visual_reference:
  face: face.png
  front: front.png
  side: side.png
  back: back.png

voice:
  model: qwen3-tts-mlx
  voice_id: female_01

style:
  realism: photorealistic
```

目录：

``` text
characters/
└── character_001/
    ├── face/
    ├── fullbody/
    ├── reference/
    ├── expressions/
    └── voice/
```

所有 Shot 都引用：

``` text
character_001
```

而不是每次重新描述角色。

目标：

-   角色视觉一致性
-   音色一致性
-   服装一致性
-   资产复用
-   Agent 自动化

------------------------------------------------------------------------

# 9. Scene System

每个场景拥有唯一：

``` text
scene_id
```

示例：

``` yaml
scene_id: scene_003

name: Tokyo Night Street

location: Tokyo

time: night

weather: rain

lighting:
  type: neon
  intensity: low

visual_style:
  realism: photorealistic
  cinematic: true

references:
  - scene_003_reference_01.png
  - scene_003_reference_02.png
```

目录：

``` text
scenes/
├── scene_001/
├── scene_002/
└── scene_003/
```

------------------------------------------------------------------------

# 10. Storyboard / Shot System

电影由大量 Shot 组成。

每个 Shot 都应该是独立、可追踪、可重新生成的任务单元。

``` yaml
shot_id: S001

scene_id: scene_003

characters:
  - character_001

duration: 5

camera:
  shot_type: close_up
  movement: slow_push_in
  angle: eye_level

lighting:
  type: night
  style: cinematic

prompt: >
  A young woman standing alone
  on a rainy Tokyo street at night.

references:
  character:
    - character_001_face.png
    - character_001_fullbody.png

  scene:
    - scene_003.jpg

first_frame:
  - S001_first.png

last_frame:
  - S001_last.png

audio:
  ambience:
    - rain.wav

video_model:
  name: h3.c
```

------------------------------------------------------------------------

# 11. 3D Director Stage

3D 导演台用于：

-   人物位置
-   人物朝向
-   场景布局
-   摄像机位置
-   摄像机方向
-   景别
-   镜头运动
-   灯光
-   Blocking
-   Previs

概念：

``` text
3D Director Stage
       │
       ├── Character A
       ├── Character B
       ├── Props
       ├── Environment
       └── Camera
              │
              ▼
            Shot
```

最终将导演台信息转换成：

``` text
Camera
+
Character Blocking
+
Scene
+
Lighting
+
Prompt
```

再交给 Image Engine / Video Engine。

------------------------------------------------------------------------

# 12. Voice Engine

## Qwen3-TTS MLX

Qwen3-TTS MLX 作为主要 AI 演员 / 配音引擎。

负责：

-   角色对白
-   多角色声音
-   音色
-   情绪
-   语速
-   多语言
-   方言
-   Voice Cloning

工作流程：

``` text
Script
  ↓
Character
  ↓
Dialogue
  ↓
Voice Parameters
  ↓
Qwen3-TTS MLX
  ↓
WAV
```

示例：

``` yaml
character_id: character_001

voice_id: female_01

language: zh

emotion: angry

speed: 1.05

dialogue: "你到底还要骗我多久？"
```

输出：

``` text
S001_character_001_dialogue.wav
```

------------------------------------------------------------------------

# 13. Music Engine

## ACE-Step 1.5

ACE-Step 1.5 作为音乐 / OST 引擎。

负责：

-   BGM
-   OST
-   场景音乐
-   情绪音乐
-   片头音乐
-   片尾音乐
-   氛围音乐

工作流程：

``` text
Scene
  ↓
Emotion
  ↓
Genre
  ↓
Tempo
  ↓
ACE-Step 1.5
  ↓
WAV
```

示例：

``` yaml
scene_id: scene_003

emotion: lonely

genre: cinematic

tempo: slow

duration: 45s
```

输出：

``` text
scene_003_bgm.wav
```

------------------------------------------------------------------------

# 14. Video Engine

## h3.c

h3.c 作为核心本地视频生成引擎。

优先采用：

> **Native Apple Silicon + Metal**

而不是把 PyTorch MPS 作为核心视频推理路径。

------------------------------------------------------------------------

## 14.1 视频生成输入

``` text
Shot
 │
 ├── Prompt
 ├── Character Reference
 ├── Scene Reference
 ├── First Frame
 ├── Last Frame
 ├── Reference Image
 ├── Reference Video
 ├── Audio
 └── Camera Description
```

进入：

``` text
h3.c
 ↓
Metal
 ↓
Video
```

------------------------------------------------------------------------

# 15. h3.c Gateway 设计

建议不要让影策直接调用 h3.c CLI。

采用：

``` text
影策
 ↓
AI Media Gateway
 ↓
H3 Worker / H3 Server
 ↓
h3.c
 ↓
Metal
```

例如：

``` http
POST /v1/video/generate
```

请求：

``` json
{
  "shot_id": "S001",
  "prompt": "A young woman standing alone on a rainy Tokyo street at night.",
  "duration": 5,
  "first_frame": "/assets/S001_first.png",
  "last_frame": "/assets/S001_last.png",
  "reference_images": [
    "/assets/character_001_face.png",
    "/assets/scene_003.jpg"
  ]
}
```

Worker 负责把统一 API 转换为 h3.c 所需参数。

------------------------------------------------------------------------

# 16. Asset Store

所有项目素材统一管理。

``` text
/project
│
├── script/
│
├── characters/
│   │
│   ├── character_001/
│   │   ├── face/
│   │   ├── fullbody/
│   │   ├── reference/
│   │   ├── expressions/
│   │   └── voice/
│   │
│   └── character_002/
│
├── scenes/
│   ├── scene_001/
│   ├── scene_002/
│   └── scene_003/
│
├── storyboard/
│
├── shots/
│   │
│   ├── S001/
│   │   ├── prompt.json
│   │   ├── reference/
│   │   ├── image/
│   │   ├── video/
│   │   ├── voice/
│   │   └── audio/
│   │
│   └── S002/
│
├── audio/
│
└── final/
```

------------------------------------------------------------------------

# 17. Asset Metadata

建议每个 Asset 都拥有：

``` yaml
asset_id: asset_000001

project_id: project_001

type: image

subtype: character_reference

character_id: character_001

scene_id: null

shot_id: null

model:
  name: iris.c
  version: flux-klein-4b

prompt: "..."

created_at: "2026-09-04T00:00:00"

source:
  type: generated

parent_assets:
  - asset_000000
```

形成完整追踪链：

``` text
Asset
 ↓
Version
 ↓
Generation
 ↓
Shot
 ↓
Scene
 ↓
Episode
 ↓
Project
```

------------------------------------------------------------------------

# 18. Agent Architecture

Agent 位于影策之上。

``` text
Codex / Claude Code / Qwen
             │
             ▼
        影策 Agent
             │
          MCP/API
             │
             ▼
     AI Media Gateway
             │
     ┌───────┼────────┐
     ▼       ▼        ▼
   Image   Voice     Video
```

Agent 可以执行：

``` text
读取剧本
 ↓
分析角色
 ↓
创建角色
 ↓
创建场景
 ↓
生成分镜
 ↓
建立 Shot
 ↓
生成参考图
 ↓
生成视频
 ↓
生成对白
 ↓
生成 BGM
 ↓
检查结果
 ↓
重新生成失败镜头
```

------------------------------------------------------------------------

# 19. Agent 自动生产示例

用户输入：

``` text
把第3集第8个镜头改成夜景。

女主角情绪改成悲伤。

保持角色脸部和服装一致。

重新生成画面、视频、对白和BGM。
```

Agent 自动执行：

``` text
读取 S008
    ↓
修改 Scene
    ↓
修改 Character Emotion
    ↓
Image Generation
    ↓
H3 Video Generation
    ↓
Qwen3-TTS MLX
    ↓
ACE-Step 1.5
    ↓
更新 Asset
    ↓
更新影策
```

------------------------------------------------------------------------

# 20. FFmpeg / Post Production

所有媒体生成完成后：

``` text
Video
 +
Dialogue
 +
BGM
 +
Ambience
 +
SFX
      ↓
    FFmpeg
      ↓
 Final Movie
```

负责：

-   视频拼接
-   音频混合
-   音量控制
-   淡入淡出
-   字幕
-   FPS
-   分辨率
-   编码
-   音画同步
-   多轨道混音

------------------------------------------------------------------------

# 21. 完整生产流水线

``` text
                    ┌──────────────┐
                    │    Script    │
                    │     剧本      │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  Characters  │
                    │     角色      │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │    Scenes    │
                    │     场景      │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  Storyboard  │
                    │     分镜      │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ 3D Director  │
                    │     导演台     │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ Image Engine │
                    │   生图模型     │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │    h3.c      │
                    │ Video Engine │
                    └──────┬───────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
      ┌──────────────┐           ┌──────────────┐
      │ Qwen3-TTS MLX  │           │ ACE-Step 1.5 │
      │     配音      │           │      BGM      │
      └──────┬───────┘           └──────┬───────┘
             │                           │
             └─────────────┬─────────────┘
                           ▼
                    ┌──────────────┐
                    │    FFmpeg    │
                    │    Editing   │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ FINAL MOVIE  │
                    │      🎬      │
                    └──────────────┘
```

------------------------------------------------------------------------

# 22. 推荐技术栈

  层                技术
  ----------------- ----------------------------
  Hardware          Mac M5 Pro 48GB
  Director UI       影策 / Open AI Canvas
  Agent             Codex / Claude Code / Qwen
  Agent Protocol    MCP
  AI Gateway        Go
  API               REST / WebSocket
  Queue             Redis
  Database          SQLite → PostgreSQL
  Asset Storage     Local Filesystem
  Image Engine      iris.c
  Voice             Qwen3-TTS MLX
  Music             ACE-Step 1.5
  Video             h3.c + Metal
  Post Production   FFmpeg

------------------------------------------------------------------------

# 23. Model Abstraction

Gateway 不绑定具体模型。

## Image

``` text
/v1/image/generate
        │
        ▼
Image Worker
        │
        └── iris.c
              └── FLUX.2 Klein 4B
```

## Voice

``` text
/v1/voice/generate
        │
        ▼
Voice Worker
        │
        └── mlx-audio
              └── Qwen3-TTS MLX
```

## Music

``` text
/v1/music/generate
        │
        ▼
Music Worker
        │
        ├── ACE-Step 1.5
        └── Future Music Models
```

## Video

``` text
/v1/video/generate
        │
        ▼
Video Worker
        │
        ├── h3.c
        └── Future Video Models
```

因此：

> **影策永远不需要知道底层到底使用什么模型。**

------------------------------------------------------------------------

# 24. 推荐项目结构

AI Media Gateway 推荐使用 Go：

``` text
ai-media-gateway/
│
├── cmd/
│   └── server/
│
├── internal/
│   ├── api/
│   ├── gateway/
│   ├── scheduler/
│   ├── queue/
│   ├── workers/
│   │   ├── image/
│   │   ├── voice/
│   │   ├── music/
│   │   └── video/
│   │
│   ├── models/
│   ├── assets/
│   ├── jobs/
│   ├── storage/
│   └── config/
│
├── pkg/
│   └── types/
│
├── configs/
├── scripts/
├── Dockerfile
└── go.mod
```

------------------------------------------------------------------------

# 25. Worker Interface

所有模型 Worker 使用统一接口。

概念：

``` go
type MediaWorker interface {
    Generate(ctx context.Context, req GenerateRequest) (*GenerateResult, error)
    Status(ctx context.Context, jobID string) (*JobStatus, error)
    Cancel(ctx context.Context, jobID string) error
}
```

不同模型只需要实现自己的 Worker：

``` text
ImageWorker
VoiceWorker
MusicWorker
VideoWorker
```

------------------------------------------------------------------------

# 26. Job 数据结构

``` yaml
job_id: job_000001

project_id: project_001

episode_id: episode_003

shot_id: S008

type: video

status: queued

priority: normal

worker: h3

input:
  prompt: "..."
  references:
    - asset_001
    - asset_002

output:
  assets: []

created_at: "2026-09-04T00:00:00"

started_at: null

completed_at: null

error: null
```

状态：

``` text
queued
  ↓
running
  ↓
completed
```

失败：

``` text
running
  ↓
failed
  ↓
retry
```

------------------------------------------------------------------------

# 27. Agent + MCP

推荐：

``` text
Codex / Claude Code / Qwen
              │
              ▼
             MCP
              │
              ▼
            影策
              │
              ▼
     AI Media Gateway
```

Agent 可以拥有：

``` text
create_project
create_character
create_scene
create_shot

generate_image
generate_voice
generate_music
generate_video

get_job_status
get_asset
replace_asset

update_shot
update_character
update_scene
```

最终形成：

> **AI 副导演 API**

------------------------------------------------------------------------

# 28. 一个完整 Shot 的生命周期

``` text
Create Shot
     ↓
Analyze Script
     ↓
Select Character
     ↓
Select Scene
     ↓
Create Reference
     ↓
Generate Image
     ↓
Human / Agent Review
     ↓
Generate Video
     ↓
Generate Dialogue
     ↓
Generate BGM
     ↓
Mix Audio
     ↓
Review
     ↓
Approve
     ↓
Final Shot
```

------------------------------------------------------------------------

# 29. 人机协作模式

不是所有内容都应该完全自动化。

推荐：

``` text
AI
 ↓
生成
 ↓
Human Review
 ├── Approve
 └── Revise
       ↓
      AI
```

三个关键审核点：

### 角色审核

``` text
Face
Hair
Body
Clothing
Voice
```

### 分镜审核

``` text
Composition
Camera
Blocking
Lighting
```

### 成片审核

``` text
Video
Dialogue
BGM
Audio Sync
Continuity
```

------------------------------------------------------------------------

# 30. 一致性系统

## Character Consistency

``` text
Character ID
      │
      ├── Face Reference
      ├── Full Body
      ├── Clothing
      ├── Expression
      └── Voice
```

## Scene Consistency

``` text
Scene ID
      │
      ├── Location
      ├── Lighting
      ├── Weather
      ├── Props
      └── Reference
```

## Shot Continuity

``` text
Shot
 │
 ├── Character
 ├── Scene
 ├── Camera
 ├── Lighting
 ├── Reference
 └── Previous Shot
```

最终目标：

> Character Consistency + Scene Consistency + Shot Continuity

------------------------------------------------------------------------

# 31. M5 Pro 48GB 运行策略

重点不是单纯追求：

``` text
同时运行最多模型
```

而是：

``` text
稳定完成整条生产流水线
```

推荐：

``` text
                    M5 Pro 48GB
                         │
                  Resource Manager
                         │
       ┌─────────────────┼─────────────────┐
       │                 │                 │
       ▼                 ▼                 ▼
    Image             Voice             Video
       │                 │                 │
       └─────────────────┼─────────────────┘
                         │
                    Queue / Scheduler
```

原则：

1.  大模型任务优先排队
2.  视频生成优先级高
3.  避免多个大型 DiT 同时运行
4.  任务完成后释放 Worker
5.  保留系统内存余量
6.  使用缓存减少重复生成

------------------------------------------------------------------------

# 32. 推荐最终运行方式

``` text
Mac
 │
 ├── 影策
 │
 ├── AI Media Gateway
 │
 ├── Image Worker
 │
 ├── Qwen3-TTS MLX
 │
 ├── ACE-Step 1.5
 │
 ├── h3.c
 │
 └── FFmpeg
```

其中：

``` text
影策
```

负责 UI / Project / Director。

``` text
AI Media Gateway
```

负责 Orchestration。

``` text
Workers
```

负责 Inference。

------------------------------------------------------------------------

# 33. 最终系统定位

``` text
                         AI FILM OS
                             │
              ┌──────────────┴──────────────┐
              │                             │
        Control Plane                  Media Plane
              │                             │
            影策                      AI Media Gateway
              │                             │
      ┌───────┼───────┐          ┌──────────┼──────────┐
      │       │       │          │          │          │
    Script Character Scene      Image      Voice      Video
      │       │       │          │          │          │
      └───────┴───────┴──────────┴──────────┴──────────┘
                                      │
                                      ▼
                                Final Movie
```

------------------------------------------------------------------------

# 34. 核心角色划分

  系统               角色
  ------------------ ------------------------
  影策               AI 导演 / 制片管理
  AI Media Gateway   AI 制片调度 / 中央路由
  Image Model        美术 / 概念设计
  Qwen3-TTS MLX      演员 / 配音
  ACE-Step 1.5       作曲 / OST
  h3.c               摄影 / 视频生成
  FFmpeg             后期制作
  Agent              AI 副导演
  M5 Pro 48GB        本地 AI 工作站

------------------------------------------------------------------------

# 35. 第一阶段实施顺序

不要一次完成所有功能。

## Phase 1：基础架构

``` text
影策
 ↓
AI Media Gateway
 ↓
Job Queue
 ↓
Asset Store
```

先打通 API、任务和资产体系。

## Phase 2：视频

``` text
影策
 ↓
Gateway
 ↓
h3.c
 ↓
MP4
```

目标：

> Shot → Video

## Phase 3：声音

加入：

``` text
mlx-audio + Qwen3-TTS MLX
```

目标：

> Shot → Video + Dialogue

## Phase 4：音乐

加入：

``` text
ACE-Step 1.5
```

目标：

> Shot → Video + Dialogue + BGM

## Phase 5：Image

加入：

``` text
iris.c + FLUX.2 Klein 4B
```

目标：

> Character / Scene → Reference Image → Video

## Phase 6：Agent

加入：

``` text
Codex / Claude Code / Qwen
        ↓
       MCP
        ↓
      影策
        ↓
AI Media Gateway
```

目标：

> **自然语言 → 完整电影制作任务**

------------------------------------------------------------------------

# 36. 最终架构

``` text
                         M5 Pro 48GB
                              │
                              ▼
                     ┌────────────────┐
                     │      影策       │
                     │ AI Director OS │
                     └───────┬────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │ AI Media Gateway    │
                  │                     │
                  │ API                 │
                  │ Queue               │
                  │ Scheduler           │
                  │ Asset Manager       │
                  │ Model Router        │
                  └───────┬─────────────┘
                          │
          ┌───────────────┼────────────────┐
          │               │                │
          ▼               ▼                ▼
     Image Worker    Voice Worker     Video Worker
          │               │                │
          ▼               ▼                ▼
     iris.c / FLUX.2 Klein 4B      Qwen3-TTS MLX         h3.c
                                          Metal
          │               │                │
          ▼               ▼                ▼
        Image           Voice             Video
          │               │                │
          └───────────────┼────────────────┘
                          │
                          ▼
                    Music Worker
                          │
                          ▼
                    ACE-Step 1.5
                          │
                          ▼
                         BGM
                          │
                          ▼
                    ┌───────────┐
                    │  FFmpeg   │
                    │ Post Prod │
                    └─────┬─────┘
                          ▼
                    ┌───────────┐
                    │   🎬      │
                    │ FINAL     │
                    │ MOVIE     │
                    └───────────┘
```

## 一句话总结

> **影策负责"导演什么"，AI Media Gateway 负责"怎么调度"，各 Worker
> 负责"怎么生成"，FFmpeg 负责"怎么成片"。**

最终目标不是单纯做一个 AI 生图工具或 AI 视频生成器，而是：

> **构建一个运行在个人 Apple Silicon 工作站上的本地 AI Film OS。**

核心目标：

``` text
ONE PERSON
    ↓
AI Director OS
    ↓
Script → Character → Scene → Storyboard
    ↓
Director Stage
    ↓
Image → Video → Voice → Music
    ↓
Post Production
    ↓
FINAL MOVIE
```
