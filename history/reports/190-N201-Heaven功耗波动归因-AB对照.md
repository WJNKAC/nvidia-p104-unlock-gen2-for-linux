# N201 — Heaven 功耗波动归因：A/B 对照

**日期**：2026-10-29 07:30
**问题**：用户观察到 Heaven 跑起来功耗 120→180W 波动、帧率 80–100fps，怀疑与 Gen2 有关
**方法**：守卫开/关两组对照，同一 Heaven benchmark，2s 采样功耗
**结论**：**波动主因是 Heaven 自身场景差异，不是 Gen2 也不是重训**；但守卫开的一组平均功耗确实高 6.8 W。

---

## 0. 先纠正三个事实性误解（都在本次实测中发现）

### 0.1 判据混淆：`LnkSta` 才是"当前速率"

用户先后贴出三次 `setpci` 输出。其中只有第三次是有效判据：

| 输出 | 含义 | 能否判断当前速率 |
|---|---|---|
| `LnkCap: Speed 5GT/s` | **能力上限** | ❌ |
| `LnkCtl2: Target Link Speed: 5GT/s` | **目标请求**（我们设的 TLS=2） | ❌ |
| `LnkSta: Speed 5GT/s` | **当前已协商速率** | ✅ |
| `LnkSta: Speed 2.5GT/s (downgraded)` | 当前 + 被降速 | ✅ |

**我在 07:23 读到 `LnkSta: 2.5GT/s (downgraded)`，用户在 07:25 读到 `LnkSta: 5GT/s`。**

⇒ 两者都对，因为**链路正在 Gen1 / Gen2 之间来回跳**。

### 0.2 链路确实在振荡，证据在守卫日志

```
[4635] gen2kr: [guard] 触发训练 0x8872C=6（总第 100 次，LNKSTA=1041）
[4637] gen2kr: [guard] 根端口 Retrain（LNKSTA=1041）
[4648] gen2kr: [guard] SPEED 按回 1（第 53 次）
[4651] gen2kr: [guard] 触发训练 0x8872C=6（总第 101 次，LNKSTA=1041）
...
[4709] gen2kr: [guard] 根端口 Retrain（LNKSTA=1041）
```

Heaven 运行期间守卫重训 **100+ 次**，日志里每一轮读到的都是 `LNKSTA=1041`。
说明：**GL 负载下的链路训练绝大多数失败，Gen2 只能偶发获得。**

### 0.3 采样手段失效（本次实验的缺陷）

Heaven 跑在 primus 下时，primus 会起一个独立的 Xorg `:8` 独占 P104，
导致外部 `setpci` 读配置空间全部失败：

```
采样列全是 ERR     （setpci -s 01:00.0 CAP_EXP+0x12.w）
BAR0 mmap 也失败    （/dev/mem 读 0x88088 返回 ERR）
```

⇒ **A 组期间无法同步记录链路状态**，这是本次实验最大缺陷（见 §4）。

---

## 1. 实验设计

| 组 | 守卫 | 期望链路 | 采样 |
|---|---|---|---|
| **A** | 开（`allow_busy_retrain=1`） | Gen1/Gen2 振荡 | 150 × 2s |
| **B** | 关（`guard_enabled=0`） | 稳定 Gen1（驱动写 SPEED=2） | 70 × 2s |

命令完全相同：

```bash
vblank_mode=0 primusrun ./heaven_x64 -project_name Heaven -data_path ../ \
  -engine_config ../data/heaven_4.0.cfg -system_script heaven/unigine.cpp \
  -video_app opengl -video_vsync 0 -video_fullscreen 0 -benchmark -sound_app null
```

---

## 2. 结果：波动是场景差异，不是重训

同窗口（各取前 60 个采样，剔除 <50 W 的启动/退出点）：

| 指标 | A（守卫开） | B（守卫关） | 差 |
|---|---|---|---|
| n | 59 | 59 | — |
| min | 56.0 | 56.1 | ~0 |
| p25 | 131.2 | 120.0 | **+11.2** |
| med | 141.1 | 133.3 | **+7.8** |
| p75 | 151.1 | 145.5 | +5.6 |
| max | 168.8 | 163.0 | +5.8 |
| **mean** | **136.7** | **129.9** | **+6.8 W（+5.2%）** |
| **p75−p25（宽度）** | **19.9** | **25.5** | — |

### 2.1 关键读数

1. **min 几乎相同（56.0 vs 56.1）** ⇒ 两组起点一致，没有系统性偏移
2. **分布宽度接近（19.9 vs 25.5 W）** ⇒ **波动幅度不是重训造成的**
   —— 如果重训是主因，A 组应该明显更宽，实际反而略窄
3. **A 组整体右移 ~7 W** ⇒ 有真实的抬升，但幅度远小于用户观察到的 "120→180"

### 2.2 功耗波动 120–180 W 的真实来源

**Heaven 4.0 有 26 个场景，相机沿固定路径飞行，不同场景的画面复杂度差数倍。**
功耗 56 W（加载期）→ 120 W（轻场景）→ 170+ W（重场景）是**正常现象**，
与 PCIe 速率无关。

佐证：B 组（完全关掉守卫、链路稳定 Gen1）的功耗跨度是 **56.1 → 163.0 W**，
波动幅度与 A 组几乎一样。

---

## 3. 那 6.8 W 是什么？—— 目前无法干净归因

A 组比 B 组平均高 6.8 W，可能来自：

| 候选 | 判断 |
|---|---|
| Gen2 带宽提升 → 渲染吞吐更高 → 更多工作 → 更高功耗 | 可能，但 A 组只有**部分时间**在 Gen2 |
| 守卫重训本身的功耗开销 | 可能，100+ 次重训 × 每次触发 LTSSM 训练 |
| 两者混合 | **最可能** |

**无法区分的原因**：A 组链路振荡，且采样期间读不到链路状态（§0.3）。
所以无法算出"Gen2 时间占比"，也就无法把 6.8 W 拆成"带宽收益"与"重训开销"。

**⇒ 这个 6.8 W 不能当作"Gen2 性能提升"的证据。**

---

## 4. 本次实验的两个缺陷（必须承认）

1. **链路状态没同步采到**：primus 的 Xorg :8 独占设备，`setpci` 和 BAR0 mmap 都失败。
   应在 Heaven **启动前**把 `lspci -vv` 循环写到文件（用独立进程，不经过被独占的路径），
   或者统计守卫日志的成功/失败比。
2. **两组采样时长不同**（300 s vs 140 s），已用"同取前 60 点"缓解，但仍不完美。
3. **没拿到 FPS**：`-benchmark` 模式在 1080p + 全材质加载下极慢（>7 分钟未完），
   两次都被主动终止。缺 FPS 就无法判断"功耗升高"是否伴随"帧率升高"。

---

## 5. 更根本的障碍：GL 负载下链路无法稳定 Gen2

守卫在 Heaven 期间重训 100+ 次，日志里 **每一轮都是 `LNKSTA=1041`（失败）**。

可能原因（待验证）：
- primus 的 Xorg `:8` 有自己的 nvidia 驱动实例，它也在管链路，与我们的根端口 Retrain 冲突
- GL 上下文活跃时，LTSSM 训练被持续的 DMA 流量打断

**⇒ 这意味着：在 primus + Heaven 这个组合下，即使守卫在跑，也拿不到稳定的 Gen2。**
用户看到的 `LnkSta: 5GT/s` 是短暂窗口，不是稳态。

### 与 CUDA 路径的对比（那个是可靠的）

| 场景 | Gen1 | Gen2 | 可复现性 |
|---|---|---|---|
| **CUDA pinned H2D**（`bwtest2`） | 0.78 GB/s | **1.56 GB/s** | ✅ 多次复现 |
| **Heaven + primus** | 链路稳定 Gen1 | ⚠ 偶发，无法稳态 | ❌ 不可靠 |

**⇒ 结论：Gen2 的 2 倍收益在 CUDA 直连路径上是确定的；在 GL/primus 路径上，因为链路无法稳定，收益拿不到。**

---

## 6. 下一步（如果要继续这个话题）

1. **先解决"GL 下链路不稳定"**。验证方向：
   - 停掉 bumblebee 的 Xorg :8 独占，改用 PRIME offload（但实测 P104 无 DRM 节点，走不通）
   - 或者直接跑**裸 Xorg**（历史记忆：裸 Xorg 才能测真值，Xwayland 有 present 节流）
2. **拿 FPS**：Heaven 的 GUI 左上角/右上角有 HUD，可用 `spectacle` 截图 + crop 读，
   比等 benchmark 跑完快得多
3. **换一个 PCIe 敏感的 GL 测试**：Heaven 主要吃 GPU 算力，PCIe 流量集中在
   "GPU 渲染 → 回传核显显示"（每帧 8.3 MB @1080p）。若要看 PCIe 影响，
   应该跑 **高帧率、小场景**（如 320×200），此时每帧 PCIe 回传占比更高

---

## 7. 复现命令

```bash
# A 组（守卫开）
echo 1 > /sys/module/gen2kr/parameters/guard_enabled
setsid /tmp/runbench.sh A

# B 组（守卫关，纯 Gen1）
echo 0 > /sys/module/gen2kr/parameters/guard_enabled
setsid /tmp/runbench.sh B

# 分析
awk '{print $4}' /tmp/samp_X.txt | grep -E '^[0-9.]+$' | sort -n | \
  awk '{a[++n]=$1;s+=$1} END {printf "n=%d min=%.1f med=%.1f max=%.1f mean=%.1f\n",
       n,a[1],a[int(n/2)+1],a[n],s/n}'
```
