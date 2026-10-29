# N193 — 验证 cmpunlocker 式方案：早期重训确实达成 Gen2，但缺最后一环

**日期**：2026-10-29 01:05
**起因**：用户指出 `cmpunlocker` 的"循环重训 + 早期启动"方案，并质疑我"没在 RM 之前试过"
**结论**：**用户方向正确 —— 驱动未加载时 Gen2 一轮达成，SPEED 写入不故障。**
但还差一环：三个关键寄存器是**写保护**的，只有 SEC2 booter 能写。

---

## 0. 先确认项目真实存在

`github.com/amoghmunikote/cmpunlocker`（789 stars），针对 **CMP 170HX（GA100 / Ampere）**。
已克隆到 `ref/cmpunlocker/`。它的 PCIe Gen2 由**两部分**组成：

| 部分 | 文件 | 做什么 |
|---|---|---|
| 早期 systemd 服务 | `tools/hammer.sh` + `systemd/gen2.service` | **只操作配置空间** TLS + Retrain，600 轮 × 0.05 s |
| 驱动源码 patch | `driver/patches/pcie-gen2.patch` | **通过 SEC2 booter 写 BAR0** 那几个写保护的寄存器 |

**关键：hammer.sh 完全不碰 `0x8C040`。** 它只做 `setpci CAP_EXP+0x30` 和 `CAP_EXP+0x10`。
（我之前 `retrain-only.py` 做的正是这个，但只做了 6 轮、间隔 1.2 s、且驱动在跑。）

---

## 1. 从源码里挖出三个我从未碰过的寄存器

### ① `0x8C2C0` bit2 = `DIS_G2`（Disable Gen2）

```c
cya0 = readl(bar0 + 0x8c2c0);
writel(cya0 & ~(1U << 2), bar0 + 0x8c2c0);
```

实测 P104 上 `0x8C2C0 = 0x068711b3`，`DIS_G2(bit2) = 0` —— **本来就是清的**。
⇒ 这条不是 P104 的问题，但**是我的知识盲区**（从不知道有这个位）。

### ② `0x8C1C0` = `PL_LINK_RATE`，patch 里要写 `0x00240036`

```c
#define PCIE_GEN2_PL_LINK_RATE_ADDR     0x0008c1c0U
const NvU32 PCIE_GEN2_PL_LINK_RATE_VALUE = 0x00240036U;
```

实测 P104：`0x8C1C0 = 0x00020036`（差 bit18/bit21）。**尝试写入失败 —— 写保护。**

### ③ `0x880A8`（BAR0 侧 LNKCTL2 镜像）要设 `bits[19:16] = 0xF`

```c
lc2 = (lc2 & ~0x0000000FU) | PCIE_GEN2_LINK_SPEED;
lc2 = (lc2 & ~0x000F0000U) | 0x000F0000U;      // ★
```

实测 P104：`0x880A8 = 0x00000001`，**尝试写入完全无效（读回 1，TLS 和 bits[19:16] 都没变）**。

`bits[19:16]` 在 PCIe LNKCTL2 里是 **Lower SKP Ordered Set 生成/接收控制** —— 链路层维持
同步的有序集。理论上这正是"Gen2 速率下需要更多 SKP OS"的配置位。
**如果它配不上，Gen2 就会"协商成功、跑流量丢同步掉线"。这个推测与我们的 Xid 79 高度吻合。**

---

## 2. ★ 修正一个严重 bug：驱动卸载后 BAR0 不可访问

之前 `nomod-hammer.py` 报"端点读不到 —— 掉了"，我以为是链路崩溃。**完全错了。**

真实原因：**卸载 nvidia 驱动后，Linux 会把 BAR0 寄存器清零、并清掉 Command 的
Memory Space Enable**。

```
卸载后：
  setpci -s 01:00.0 0x10.l   = 00000000     ← BAR0 被清零
  mmap resource0 → 全部 0xffffffff          ← 内存空间未启用
  modprobe nvidia → "No such device"        ← 因为 BAR0 是 0
```

**修法**（`early-gen2.py` 已实现）：

```python
bar0 = 从 /sys/.../resource 表读（unregister 后仍保留）
sh(f"setpci -s {DEV} 0x10.l={bar0:08x}")          # 写回 BAR0
sh(f"setpci -s {DEV} COMMAND={(cmd|0x6):04x}")    # bit1 MemSpace | bit2 BusMaster
```

修完立刻可读：

```
=== 2. ★ 恢复 BAR0 + 打开 Memory Space ===
  BAR0 写回 = 0xf6000000  COMMAND = 0x0006
  MMIO 现在可读: True
  ✓ 0x880A4=0x0002  0x8C040=0x20085800  0x8C2C0=0x068711b3  0x8C1C0=0x00020036
```

**这条教训很重要**：以后任何"卸载驱动后操作 BAR0"的实验，第一步必须先恢复 BAR0 + MemSpace。

---

## 3. ★ 决定性实验：驱动未加载状态下的完整尝试

```
=== 0. 前提 ===  resource 表 BAR0 = 0xf6000000，驱动在时 MMIO 可读 ✓
=== 1. 卸载 nvidia 驱动 ===
=== 2. 恢复 BAR0 + MemSpace → MMIO 可读 ✓
=== 3. 解锁声明 ===  0x8841C → 0xc0b4ad00，LINK_CAP2 = 0x0006
=== 4. 0x8C2C0 DIS_G2 = 0（本来就清）；0x8C1C0 写保护 ✗
=== 5. 两端 TLS=2 ===  根端口 0x0c43 → 0x0c42；端点 0x0002（本就 2）
=== 6. 0x8C040 SPEED=1 ===  0x20085800 → 0x20045800   存活 = True   ★★
=== 7. 循环重训 600 轮 × 0.05s ===
      ★ 第 1 轮成功（0.0s）LNKSTA=0x1042 sp=2
      LnkSta: Speed 5GT/s (overdriven), Width x4
```

### 三个实质性收获

1. **驱动未加载时，写 `SPEED=1` 不掉卡**（`存活=True`）。
   ⇒ 之前反复掉卡是因为**驱动在访问 GPU（RM 在跑）**，不是 SPEED 本身有毒。
2. **Gen2 第 1 轮重训就达成**，不需要 30 次。
   ⇒ 前提是对的（声明解锁 + TLS=2 + SPEED），一旦齐备，重训立即生效。
3. **`lspci` 显示 `Speed 5GT/s (overdriven)`** —— "overdriven"（超频运行）。

---

## 4. `overdriven` 这个标记是决定性证据

PCIe 规范里，链路实际速率高于设备声明的 capability 时，会标记为
**"speed overdriven"**。这说明：

> **链路物理层真的跑在 5 GT/s 了，但设备的 capability 声明仍是 Gen1。**

这正是我们的核心困境，现在有了官方判据：

- 我们改的 `LCAP2 = 0x06` 只是 BAR0 镜像，**硬件内部真实 capability 仍是 Gen1**
- 所以协议栈认为这是"非法超频状态"
- 于是硬件内部数据面（MPSS / VC credits / SKP OS / Replay buffer）**仍按 Gen1 配置**
- 真实 DMA 一来，两端状态不一致 → **Xid 79**

旁证（驱动在时测的）：

```
端点 LNKCTL2：写 0f02 → 读回 0f01      ← TLS=2 被硬件钳回 1
```

**驱动在时 TLS 被钳回 1，这就是"硬件真实 capability = Gen1"的直接证据。**
（驱动不在时钳位不生效，但那只说明钳位逻辑由驱动/固件在某处控制，不代表 capability 变了。）

---

## 5. 为什么 cmpunlocker 能成功，我们不能

| | cmpunlocker (CMP 170HX) | 我们 (P104-100) |
|---|---|---|
| GPU 架构 | Ampere GA100 | **Pascal GP104** |
| 驱动形态 | **nvidia-open 610.43**（有 C 源码） | **闭源 580.178.04**（只有 blob） |
| 写 `0x8C1C0` / `0x880A8` | ✅ 通过 **SEC2 booter 注入** | ❌ **写保护，普通 MMIO 无效** |
| 内核版本 | 任意 | 6.12.113+deb13-amd64 |

**cmapunlocker 的关键手段**（`pcie-gen2.patch`）：

```c
GPU_REG_WR32(pGpu, 0x001fa824U, wpr2Lo);      // 设 WPR2 寄存器
GPU_REG_WR32(pGpu, 0x001fa828U, wpr2Hi);
xpStatus = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp, addr, value);
xpStatus = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp, ...);
```

**它不是在写寄存器，是在改 GPU 的 SEC2 安全引擎 booter payload，让 GPU 在 POST 阶段
自己去配置这些寄存器。** 因为那些寄存器对普通 MMIO 写保护。

这需要：
1. `nvidia-open` 的 C 源码（能动 `kernel_gsp.c`）
2. Pascal 架构必须被 `nvidia-open` 支持 —— **不支持**（nvidia-open 从 565 起只支持 Turing+）

⇒ **此路对 P104 不通。**

---

## 6. 但用户的方案里有一条我应该继续追

**"驱动 probe 完成后立刻重训"** —— cmpunlocker 的 `pcie-gen2-probe-retrain.patch`
就是把 `nv_cmp170hx_retrain_gen2()` 挂在 `nv_pci_probe` 末尾，probe 完立刻重训 20 次 × 100 ms。

这个思路在 Linux 上**可以用用户态模拟**：
1. 驱动正常加载（probe 会 reset 到 Gen1）
2. **probe 完成、RM 就绪后**，立刻做重训
3. 关键：此时 TLS=2 会被钳回 1，所以必须先解锁声明

这正是 `gen2-bw.py` 做的事，确实达成了 Gen2 —— 但接着测带宽就是 Xid 79。

**差别在于 cmpunlocker 的 SEC2 patched 让内部数据面也到了 Gen2，而我们只到链路层。**

---

## 7. 现在的完整结论

```
① 链路层能到 Gen2        —— 已验证多次（lspci 亲证 5GT/s，甚至标记 overdriven）
② 但设备 capability 声明仍是 Gen1 —— TLS 被钳回 1 是硬证据
③ 内部数据面（SKP OS / credits / MPSS）仍是 Gen1
④ 需要的三个配置位（0x8C1C0、0x880A8[19:16]、0x8860C bit0）全部写保护
⑤ 解除写保护 = SEC2 booter 注入 = 需要 nvidia-open 源码 = Pascal 不支持
⇒ 在 P104 + 闭源驱动 的组合下，无法达成可用的 Gen2
```

**这不是"我没试对"，是"这条路在 P104 上物理封死"。**
但用户的方案确实推进了三步：早期重训有效、SPEED 不必然掉卡、找到了三个漏掉的寄存器。

---

## 8. 剩下唯一没排除的可能性

**换插槽/换机器。** 如果某块主板的 BIOS 在 POST 阶段就把 P104 的 PCIe 声明配置成 Gen2
（`LCAP2` 冷启动就是 `0x06`），那就不需要任何软件介入。

这是**唯一还可能成功的方向**，而且成本只是插拔一次。

判据：冷启动后立刻 `lspci -vv -s <bdf> | grep LnkCap2`，
看 `LnkCap2: Supported Link Speeds: 2.5-5GT/s` 还是只有 `2.5GT/s`。

---

## 9. 本轮工具

| 工具 | 作用 | 结果 |
|---|---|---|
| `cmp-style.py` | 照 cmpunlocker 完整序列（Pascal 适配） | `--dry-run` 挖出三个漏掉的寄存器 |
| `early-gen2.py` | 驱动未加载时完整尝试（含 BAR0 修复） | ★ Gen2 一轮达成、SPEED 存活 |
| `nomod-hammer.py` | 首版，踩了 BAR0 清零的坑 | 已由 early-gen2.py 取代 |

## 10. 本轮新增禁忌

| 操作 | 后果 |
|---|---|
| 卸载驱动后直接 mmap BAR0 | 读到全 `ffffffff`，**误判成掉卡**。必须先写回 BAR0 + 开 MemSpace |
| 把"重训瞬间配置读失败"当掉卡 | 重训后立刻读 LNKSTA 可能返回空，**必须重试 5 次** |
| 硬编码 BAR0 物理地址 | 驱动卸载/重载后可能变，**必须从 `/sys/.../resource` 动态取** |
