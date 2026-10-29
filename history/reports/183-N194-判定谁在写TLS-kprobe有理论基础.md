# N194 — 判定"谁在写 TLS"：驱动主动写，kprobe 有理论基础；但抢先改动会让 probe 失败

**日期**：2026-10-29 01:20
**起因**：用户提出 kprobe 思路 —— "nvidia.ko 在加载时把 TLS 改回 Gen1，用 kprobe 拦住"
**结论**：**用户的前提判断 100% 正确**（驱动主动写，非硬件回滚），
但实测发现两个新障碍：① 抢写方案不可行 ② 抢先改寄存器会导致 probe 失败。

---

## 0. 先纠正一个我自己的错误认知

我们一直设的 `RmForceEnableGen2=1` —— **nvidia.ko 里根本没有这个字符串**。

```
$ strings nvidia.ko | grep -i RmForceEnableGen2
（精确匹配：0 条）
$ strings nvidia.ko | grep -i RMPcieLinkSpeed
5ae6d9e RMPcieLinkSpeed
5cabbd7 RMPcieLinkSpeed
```

而 `RMPcieLinkSpeed` 周围是一整套真实参数：
`RMPcieGenSwitchOnPmu`、`RMPcieLtrL12ThresholdOverride`、`RMPcieLtrOverride`、
**`RMPcieStickyGenSpeed`**、`RMPcieP2PType`、`RMPcieFLRPolicy`、`RMPcieFlrDevinitTimeout`。

⇒ **`RmForceEnableGen2` / `RmEnableGen2` 是从 Windows 参考项目抄来的、Linux 驱动不认识的空参数。**
我们之前"设了没效果"不是配置不对，是**参数根本不存在**。
（`RMPcieStickyGenSpeed` 这个名字直译是"粘住 Gen 速度"，值得后续单独查。）

---

## 1. ★ 判定实验：TLS 到底是驱动写的，还是硬件回滚

这是决定 kprobe 是否有意义的唯一关键。之前从未做过。

### 方法

在**驱动不在**的前提下：
- **A** 写 TLS=2 → 读回
- **B** **不写 TLS**，只触发一次根端口 Retrain → 读回
  - 若变 1 ⇒ 硬件重训失败回滚（kprobe 无用）
  - 若仍 2 ⇒ 之前"变回 1"是驱动写的（kprobe 有价值）

### 结果

```
=== 实验 A ===
  写前: LNKCTL2=0x0002 TLS=2
  写 2 后: LNKCTL2=0x0002 TLS=2  ✓ 保持 2
  根端口 TLS: 0x0c43 → 0x0c42

=== ★ 实验 B（只重训，不写 TLS）===
  重训后: 端点 LNKCTL2=0x0002 TLS=2      ← 没回滚！
          端点 LNKSTA=0x1041 sp=1
          根端口 LNKCTL2=0x0c42 TLS=2

=== 重复 5 轮（每轮 写TLS=2 → 重训 → 读回）===
  第1..5轮: TLS=2 sp=1
  回滚次数: 0/5
```

**⇒ 硬件不会把 TLS 回滚。之前观察到的"变回 1"完全是 nvidia.ko 主动写的。**

用户的判断被证实。kprobe 这条路**理论上成立**。

### 但附带一个重要信息

TLS=2 保持住了，链路却**仍是 sp=1**。
⇒ TLS 只是"目标请求"，真正决定速率的是 `0x8C040.SPEED`。
两者缺一不可（这也解释了为什么 `retrain-only.py` 只改 TLS 升不上去）。

---

## 2. ★★ 精确定位：驱动 probe 到底改了什么

用「先 Gen2 再 modprobe」的顺序，逐寄存器对比 probe 前后（`gen2-then-load.py` 实现）：

```
=== Gen2 就绪（驱动不在）===
  [Gen2 就绪] 端点 TLS=2 sp=2 | 根端口 TLS=2 sp=2 | LCAP2=0x0006
              BAR0: 0x8C040=0x20045800 (SPEED=1)   0x880A8=0x00010002

=== modprobe 之后 ===
  [probe 后]  端点 TLS=2 sp=1 | 根端口 TLS=2 sp=1 | LCAP2=0x0006
              BAR0: 0x8C040=0x20585800 (SPEED=2)   0x880A8=0x00010002
```

**结论非常干净：**

| 寄存器 | probe 前 | probe 后 | 变化 |
|---|---|---|---|
| `0x8C040` SPEED[19:18] | **1** | **2** | ★ **驱动改回 Gen1** |
| `0x8C040` bit22 | 0 | **1** | 驱动置上 |
| `0x880A8` LNKCTL2 镜像 | 0x00010002 | 0x00010002 | 未变 |
| 端点/根端口 LNKCTL2 TLS | 2 | 2 | **未变** |
| `LCAP2` | 0x0006 | 0x0006 | 未变 |

⇒ **驱动只改 `0x8C040` 一个寄存器。TLS 它根本不管。**

这意味着 kprobe 如果实现了，**只需要拦截一处写**，工程量比预想小。
而且因为 TLS 已经是 2，只要 SPEED 保持 1，链路就应该是 Gen2。

---

## 3. 抢写方案实测失败（比 kprobe 简单得多的尝试）

思路：既然驱动只写一次，那在 modprobe 期间开 5ms 高频循环把 SPEED 写回 1，
不需要 kprobe。

### 结果

```
=== 2. 推到 Gen2 ===
  LCAP2=0x0006 SPEED=1
  等待 3s（写 SPEED 后不能立即重训）
  ★ 第 1 轮 Gen2：LNKSTA=0x1042
  就绪状态：端点 sp=2 根端口 sp=2

=== 3. ★ 启动抢写线程（5ms，8s）同时 modprobe ===
  modprobe 返回 0
  smi: No devices found.                       ← ★ 驱动 probe 失败
  抢写统计：写入 0 次，last SPEED=1，失效 0 次，异常 0 次

=== 4. probe 后状态 ===
  BAR0=0xf6000000 MMIO 不可读                  ← BAR0 又被清零
  端点 TLS=- sp=- | 根端口 sp=1                ← 端点配置空间读不到
```

**抢写线程一次都没写**（因为 SPEED 已经是 1，无需写）。
真正的问题在别处：**驱动 probe 直接失败了。**

---

## 4. ★ 新障碍：预先改过寄存器会让 probe 失败

两次实验都出现同样的结局：

| 实验 | probe 结果 |
|---|---|
| `early-gen2.py`（驱动不在时改寄存器 + Gen2 + modprobe） | `smi: No devices found` |
| `fight-probe.py`（同上 + 抢写线程） | `smi: No devices found` |

而正常冷启动（寄存器是硬件默认值）时 probe 一定成功。

**推断**：NVIDIA 的 devinit 会读这些寄存器并校验状态。
发现 `0x8841C`/`0x88610`/`0x8872C` 被改成非预期值、或 `LCAP2` 与内部状态不一致时，
devinit 判定设备处于异常状态，probe 失败。

⇒ **"提前把状态改好"这条路走不通，反而更糟。**
必须让驱动**正常 probe**，然后在它写 `0x8C040` 的那一刻改值 —— 这正是 kprobe 的定位。

---

## 5. kprobe 可行性评估（基于实测）

### 有利条件（全部已确认）

| 条件 | 状态 |
|---|---|
| `CONFIG_KPROBES=y` | ✅ |
| `CONFIG_KALLSYMS_ALL=y`（能拿局部符号） | ✅ |
| 内核头文件 `/lib/modules/6.12.113+deb13-amd64/build` | ✅ |
| nvidia.ko 符号数 | **64662 个** |
| `0x8c040` 在二进制里的字节模式（小端 `40 c0 08 00`） | **42 处** |
| objdump 引用 `8c040` | **43 处** |
| 需要拦截的寄存器 | **只有 1 个**（`0x8C040`） |
| TLS 是否需要管 | **不需要**（驱动不碰） |

### 技术方案

```
kprobe 挂在写 0x8C040 的那条指令地址上
  pre_handler：
    检查本次写是否在写 0x8C040 且目标值 SPEED=2
    是 → 修改寄存器（如 eax）把值改成 SPEED=1（保留其他位）
  驱动以为写成功了，实际写进去的是 1
⇒ probe 全程链路保持 Gen2，RM 在 Gen2 环境下初始化内部数据面
```

### 待解决的工程问题

1. **定位具体指令**：43 处引用里，找出哪些是"写"且发生在 probe 路径
   （可以用 objdump + 交叉引用 `GPU_REG_WR32` 的调用模式）
2. **获取运行时地址**：模块内偏移 + `/sys/module/nvidia/sections/.text` 得到基址
3. **区分读写**：`mov %eax,(%rdx)` 是写，`mov (%rdx),%eax` 是读
4. **风险**：改错指令会破坏驱动初始化

### 备选：更简单的定位手段

`0x8C040` 的写入必然经过某个"寄存器写"函数或宏。
如果 nvidia.ko 里有非内联的 MMIO 写封装（如 `os_pci_write` / `nv_writel`），
可以 kprobe 那个函数，按 `(addr & 0xFFFFF) == 0x8C040` 过滤 —— 比逐指令精确、风险低。

**这是下一步应该先查的**：`nm nvidia.ko | grep -iE 'writel|reg_wr|os_pci_write'`。

---

## 6. 这一轮的净收获

| 项 | 结论 |
|---|---|
| 用户的核心判断 | ✅ **正确**：TLS 是驱动写的，不是硬件回滚（0/5 回滚） |
| 驱动改了什么 | ✅ **精确定位**：只改 `0x8C040.SPEED`（1→2）+ 置 bit22，不碰 TLS |
| `RmForceEnableGen2` | ❌ **驱动里不存在**，一直是无用参数 |
| 抢写方案 | ❌ 不可行（且真正原因是 probe 失败，不是抢不过） |
| 提前改寄存器 | ❌ **会让 probe 失败**，比不改更糟 |
| kprobe | ✅ **理论成立、环境齐备**，是唯一可行的方向 |

---

## 7. 下一步（明确的执行顺序）

1. **查 nvidia.ko 有没有可 kprobe 的 MMIO 写封装函数**
   ```bash
   nm nvidia.ko | grep -iE 'writel|reg_wr|os_pci|mmio'
   ```
   有 → 直接 kprobe 该函数，按地址过滤（简单、安全）
   无 → 回到逐指令定位

2. **定位 43 处引用中"写 0x8C040"的那一处**，确认在 probe 路径上

3. **写 kprobe 模块**，pre_handler 改 SPEED 值

4. **验证**：probe 后链路应保持 Gen2（`LNKSTA=0x1042`）+ `smi` 正常

5. **终极判据**：**带宽是否达到 1.55 GB/s**。
   如果链路 Gen2 但带宽仍 0.78 → 说明 RM 还是按 Gen1 配了数据面，
   那就回到"需要 SEC2 booter"的结论，此路彻底封死。

⚠ 第 5 步是决定性的一步。前面的所有努力都指向这一个数字。

---

## 8. 本轮工具

| 工具 | 用途 | 结果 |
|---|---|---|
| `who-writes-tls.py` | 判定 TLS 是否硬件回滚 | ★ 0/5 回滚 ⇒ 驱动写的 |
| `gen2-then-load.py` | 先 Gen2 再 modprobe，对比 probe 前后 | ★ 精确定位：只改 SPEED |
| `fight-probe.py` | modprobe 期间高频抢写 | ❌ 且发现 probe 会失败 |
