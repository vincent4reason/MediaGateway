# h3.c M5 Pro 48GB 速度优化方案（结合上游 antirez/h3.c 现状修订）

> 2026-09-05 · 基于《h3c_M5Pro_48GB_Speed_Optimization.md》+ 上游 main(8974cc0) 实测核对。
> 本地 `~/tool/h3.c` 与 origin/main **零落后**；原方案中「Phase 2 增加 token_reduction_strength」上游同样没有——是真代码改造点。

## 0. 原方案没覆盖、但上游已有的关键事实

1. **Internal canvas（render_width/render_height）**：DiT/VAE 内部小画布 + vImage 放大。
   上游验证过的缩放点：384→512（快速质量档）、320→512（激进档）。
   **参数已在 bridge 里（h3_params），worker 已于本次透传**——这是文档之外的最大独立杠杆。
2. **上游"validated balanced preset"是 steps 20 / layers 45 / reuse 2**（11 次 fresh DiT）。
   而 MediaGateway 当前默认是 **steps 6 / reuse 1（6 次评估）+ layers 45**——denoise 次数比文档首选还少。
   → benchmark 矩阵必须把「我们现有默认」作为候选，而不是假设文档配置就是基线。
3. `--reuse` 与 `--core-reuse` 互斥；`--use-int8-row-fc2` 与 `--ssd-streaming` 互斥；benchmark 关 `--show`。
4. token_reduction 仍是 `int`（ON/OFF）——可调强度确属上游未做的真改造。

## 1. 现状基线（docs/tools.md + E2E 实测）

| 项 | 值 |
|---|---|
| worker 默认 | steps 6 / layers 45 / reuse 1 / 无 token / 无 int8 / 无 internal canvas |
| 实测（864×480, 5s=120f, 6 步） | ~100-210s（含引擎加载，页面缓存热时 ~100s） |
| 影策两段式 | 草稿=同配置再 512×288；成片=864×480 |

## 2. Profile 设计（Gateway video worker + 影策两段式映射）

| Profile | steps | layers | reuse/core | token | int8 | internal canvas | 用途 |
|---|---:|---:|---|---|---|---|---|
| `draft` | 6 | 45 | reuse 1 | ON | ON | 0.625× 输出 | 影策「生成草稿」，几十秒 |
| `standard`（现默认） | 6 | 45 | reuse 1 | OFF | OFF | 无 | 快速正式 |
| `quality`（待 P0 验证） | 20 | 45 | reuse 2 | ON | ON | 无 | 文档目标配置 |
| `reference` | 20 | 50 | reuse 1 | OFF | OFF | 无 | 质量基准（仅 benchmark 用） |

worker 新透传参数：`use_int8_row_fc2`、`render_width/render_height`（32 取整）。影策侧后续把 profile 映射进 render 端点。

## 3. P0 Benchmark 矩阵（固定 prompt+seed，864×480 / 120f / 5s）

| # | 配置 | 目的 |
|---|---|---|
| R | 20/50/reuse1 | 质量基准 100% |
| A | 20/45/reuse2 | 文档首选 |
| B | A + token | token 收益（上游 24.5% 参考） |
| C | B + int8 | 叠加收益（~2.6% 参考） |
| D | 20/45/core-reuse4 | reuse 替代路线（二选一对照） |
| E | **6/45/reuse1（现默认）** | 我们的事实基线 |
| F | E + internal 0.625×（544×320→864×480） | 新杠杆 |
| G | F + token + int8 | 激进草稿上限 |

度量：
- 时间：job created→finished（含加载）+ progress 阶段耗时（denoise 段单独看）
- 质量：`ffmpeg -filter_complex ssim` 逐帧对 R；composition/主体数/脸手 **人眼 checklist**（SSIM 对构图漂移不敏感，token reduction 恰好主要伤构图——SSIM ≥0.95 且人眼过 checklist 才算 ≥95%）
- 内存：`footprint`（ps/`/usr/bin/time -l`）

产出写回 `docs/tools.md`，据此选 standard/quality 的最终参数与 internal canvas 缩放点。

## 4. P1：上游贡献（真代码改造）

1. `float token_reduction_strength`（0/0.25/0.5/0.75/1.0）——`h3_dit.c` 合并阈值暴露为参数，benchmark 定档
2. **middle-blocks-only reduction**：早期/后期 block 恢复 full tokens（保构图/脸/手），中期压缩——「≤5% 损失」最优先方向
3. 两者都以 PR 形式回馈上游 antirez/h3.c

## 5. 禁忌（沿用原方案 §13）

不改权重/VAE/DiT 架构；不 reuse3+layers40+token 三连开；reuse 与 core-reuse 互斥；int8 与 ssd 互斥；不做 SSD streaming 追速度；benchmark 关 `--show`；2K 走「低分辨率 latent + tiled VAE decode」独立项目，不动 Native DiT。

## 6. 执行顺序

```
P0  benchmark 矩阵（R/A/B/C/D/E/F/G，固定 seed，ffmpeg ssim + 人眼 checklist）→ 定 standard/quality
P0  profile 接进影策（草稿=draft、成片=standard 或 quality）
P1  token_reduction_strength + middle-blocks-only（上游 PR）→ 再 benchmark
P2  Metal kernel fusion / buffer 复用（视 P0 瓶颈再定）
```

目标：质量 ≥95%（SSIM+人眼双门槛），速度相对 reference 提升 25-40%（目标值，非承诺）。
