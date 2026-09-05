# h3.c M5 Pro 48GB：低画质损失高速优化方案

> 目标：在 Apple M5 Pro 48GB 上，以**画质损失约 ≤5%**为约束，优先提高 h3.c 视频生成速度。  
> 重点：先利用现有优化，再针对 Token Reduction 做可控改造；不要一开始重写模型或 VAE。

## 1. 核心结论

最值得优先优化的计算链：

```text
Token 数 × DiT 层数 × Denoising Pass 数
```

对应：

1. **Token Reduction**：减少每个 DiT block 要处理的 token 数。
2. **Layer Reduction**：50 → 45，减少 Transformer block 数。
3. **Denoise Reuse**：20 次 denoise → 11 次 fresh DiT evaluation。
4. **Core Reuse**：作为 `reuse` 的替代路线，不与 `reuse > 1` 同时使用。
5. **M5 INT8 FC2**：低风险额外加速，但收益较小。

不要首先修改模型权重、VAE 或直接做 2K Native DiT。

---

## 2. 第一优先：Token Reduction

### 2.1 它提高什么？

Token Reduction **不是提高画质**，而是减少 DiT 中间阶段需要处理的 token 数。

例如：

```text
原始：
100% tokens
    ↓
DiT
    ↓
DiT
    ↓
DiT

优化：
100%
 ↓
Token Reduction
 ↓
约 60~80%
 ↓
DiT
 ↓
DiT
 ↓
Restore
```

主要收益：

- 减少 DiT 计算量
- 减少 activation memory
- 减少 GPU memory read/write
- 降低部分 attention/token-dependent 运算压力
- 高分辨率时收益更加重要

### 2.2 当前 h3.c 已有实现

当前代码已经提供：

```text
--token-reduction
```

并且是在**中间 DiT blocks** 对水平相邻 video tokens 做配对/合并，同时保留 full-resolution residual。

因此不需要重写整个 DiT。

### 2.3 当前实测

官方当前 README 给出的 512×512、22 frames 测试：

```text
45 layers + reuse 2

无 Token Reduction：
16.69 s

开启 Token Reduction：
12.60 s
```

约：

```text
24.5% denoise 加速
```

但官方明确指出：

> Token Reduction 更快，但 composition 可能发生更明显变化。

因此不能简单认为 `--token-reduction` 就满足「画质只损失 5%」。

---

## 3. 最值得改的地方：Token Reduction 可调强度

目前接口主要是：

```c
int token_reduction;
```

建议增加：

```c
float token_reduction_strength;
```

例如：

```text
0.00 = OFF
0.25 = LIGHT
0.50 = BALANCED
0.75 = STRONG
1.00 = AGGRESSIVE
```

具体数值必须通过 benchmark 确定，不应直接假设。

### 目标

寻找：

```text
Quality Loss <= 5%
Speed Gain 最大
```

建议测试：

```text
0.00
0.25
0.50
0.75
1.00
```

记录：

- 总生成时间
- DiT 时间
- peak memory
- SSIM
- LPIPS
- CLIP similarity
- composition consistency
- motion consistency
- 人物/物体数量是否变化
- 面部、手部、细节稳定性

---

## 4. 第二优先：DiT Layer Reduction

当前 h3.c：

```text
50 layers = reference
45 layers = fast
40 layers = aggressive
```

对于 ≤5% 画质损失目标，优先：

```text
50 → 45
```

不要一开始：

```text
50 → 40
```

推荐：

```bash
--layers 45
```

---

## 5. 第三優先：Denoise Reuse

当前 20 steps：

```text
reuse 1 = 20 fresh DiT evaluations
reuse 2 = 11 fresh DiT evaluations
reuse 3 = 8 fresh DiT evaluations
```

优先：

```bash
--steps 20 --reuse 2
```

`reuse 3` 属于 aggressive，不作为 ≤5% 画质损失的默认方案。

---

## 6. Core Reuse

另一条路线：

```bash
--layers 45 --core-reuse 4
```

官方当前定义：

```text
core-reuse 1 = reference
core-reuse 4 = fast
core-reuse 6 = aggressive
```

重要限制：

```text
reuse > 1
```

与：

```text
core-reuse > 1
```

不能同时使用。

因此不要：

```text
--reuse 2 --core-reuse 4
```

应当二选一。

对于本项目，优先测试：

```text
方案 A：
layers 45 + reuse 2

方案 B：
layers 45 + core-reuse 4
```

然后选择画质/速度更好的方案。

---

## 7. M5 INT8 FC2

M5 可以使用：

```bash
--use-int8-row-fc2
```

这是针对 M5/Metal 4 的 INT8 FC2 路径。

当前官方测试：

```text
完整 denoiser forward
约 2.6% 加速
```

因此：

```text
Token Reduction   = 主要加速来源
Layer Reduction   = 主要加速来源
Reuse             = 主要加速来源
INT8 FC2          = 小幅额外加速
```

建议作为低风险叠加项测试。

注意：

```text
--ssd-streaming
```

不能与：

```text
--use-int8-row-fc2
```

同时使用。

M5 Pro 48GB 本身不应优先使用 SSD streaming，因为它主要解决内存压力，不是速度优化。

---

## 8. 推荐第一版高速配置

首先测试：

```bash
./h3 --profile \
  -d ./MiniMax-H3 \
  -p "YOUR PROMPT" \
  --width 512 \
  --height 512 \
  --frames 22 \
  --steps 20 \
  --layers 45 \
  --reuse 2 \
  --token-reduction \
  --use-int8-row-fc2 \
  -o outputs/test.mp4
```

但这个组合**不能直接宣称画质损失 ≤5%**。

必须和 reference 进行 A/B benchmark。

---

## 9. 推荐 Benchmark Matrix

### Reference

```text
steps = 50
layers = 50
reuse = 1
token reduction = OFF
```

作为质量基准。

### Test A

```text
steps = 20
layers = 45
reuse = 2
token reduction = OFF
```

### Test B

```text
steps = 20
layers = 45
reuse = 2
token reduction = ON
```

### Test C

```text
steps = 20
layers = 45
reuse = 2
token reduction = ON
INT8 FC2 = ON
```

### Test D

```text
steps = 20
layers = 45
core-reuse = 4
token reduction = ON/OFF
```

---

## 10. 真正的目标点

最终不是追求「参数越激进越好」，而是找到：

```text
                    Reference
                       │
                 Quality = 100%
                       │
             ┌─────────┴─────────┐
             │                   │
        Fast Candidate       Aggressive
             │                   │
        Quality ≥95%          Quality <95%
             │
             ▼
       选最快配置
```

验收标准：

| 指标 | 目标 |
|---|---:|
| 画质 | ≥95% |
| Composition | ≥95% |
| Motion | ≥95% |
| 人物/主体一致性 | ≥95% |
| 速度 | 尽量提高 |
| 模型权重 | 不修改 |
| VAE | 第一阶段不修改 |
| DiT architecture | 第一阶段不修改 |

---

## 11. 最值得做的代码修改

### Phase 1：不改核心算法

先 benchmark：

```text
50 / reuse1 / token off
45 / reuse2 / token off
45 / reuse2 / token on
45 / reuse2 / token on / INT8
45 / core-reuse4
```

找出真正瓶颈。

### Phase 2：修改 Token Reduction

增加：

```c
float token_reduction_strength;
```

实现可调强度。

### Phase 3：分阶段 Token Reduction

不要全程压缩：

```text
Early DiT
100% tokens

Middle DiT
70~85% tokens

Late DiT
100% tokens
```

这样可以减少对：

- 构图
- 人物身份
- 脸
- 手
- 运动
- 细节

的影响。

这是最值得尝试的「≤5% 画质损失」优化方向。

---

## 12. 优先级

```text
P0  Benchmark
    ↓
P0  layers 50 → 45
    ↓
P0  reuse 1 → 2
    ↓
P0  Token Reduction
    ↓
P1  Token Reduction strength
    ↓
P1  Middle-block-only reduction
    ↓
P1  M5 INT8 FC2
    ↓
P2  Metal kernel fusion
    ↓
P2  buffer / command reuse
    ↓
P3  2K tiled VAE
    ↓
P4  2K Native DiT
```

## 13. 不建议现在做

暂时不要：

- 修改模型权重
- 修改 DiT architecture
- 直接把 768p Native DiT 改成 2K
- 大幅降低 steps
- 同时打开 `reuse 3 + layers 40 + token reduction`
- 同时使用 `reuse > 1` 和 `core-reuse > 1`
- 用 SSD streaming 追求速度
- 用 `--show` 做速度 benchmark

`--show` 会增加预览解码时间，并产生约 10 GiB 临时模型驻留；正式 benchmark 应关闭。

---

## 14. 2K 的關係

h3.c 当前 H3-Base 的机械输出上限仍是：

```text
768 × 1344
```

即 768p-class canvas。

因此「2K 输出」应该作为**后续独立项目**，不能把当前 h3.c 简单改成 2048×1152 就认为完成 2K。

优先路线：

```text
低/中分辨率 DiT
      ↓
稳定视频 latent
      ↓
高分辨率 VAE / tiled decode
      ↓
2K output
```

不要第一阶段同时做速度优化和 Native 2K DiT。

---

## 15. 最终建议

针对 M5 Pro 48GB：

```text
首选：

20 steps
45 layers
reuse 2
Token Reduction（可调强度）
INT8 FC2
```

然后通过 benchmark 找出 Token Reduction 的最佳强度。

**最重要的代码改造不是重写 h3.c，而是把现有 Token Reduction 从 ON/OFF 改成「可调强度 + 中间层启用、后期恢复 full tokens」。**

目标：

```text
质量 ≥ 95%
速度提升 25~40%
```

其中「25~40%」是优化目标，不是当前代码已经保证的实测结果；实际结果必须以 M5 Pro 实机 benchmark 为准。

## 参考

- h3.c 当前仓库：https://github.com/antirez/h3.c
- 当前 README 的 Speed/Quality Preset、Token Reduction、Reuse、INT8 FC2 说明
- 当前 `h3_dit.c` / `h3.h` 中的相关参数与实现
