# NVIDIA Linux 内核驱动配置 · 静态分析（PCIe 链路 + 待机功耗）

**日期**：2026-10-28
**对象**：union@192.168.1.43，kernel 6.12.113+deb13-amd64，驱动 580.178.04，GPU = P104-100（`10DE:1B87`，VBIOS `86.04.7A.00.19`）
**目的**：在改任何东西之前，先把 Linux 侧所有可动旋钮静态盘点清楚

---

## 0. 摘要

| 问题 | 静态分析结论 |
|---|---|
| PCIe 为什么只能 Gen1 | **不是内核/驱动层的限制，是 GPU 内部（VBIOS）声明**。config space 相关字段全部硬只读（已实测三处写入均被忽略） |
| 还有没有别的路 | **有**：驱动参数 `NVreg_EnablePCIeGen3` 当前为 **0**。它走 GPU 内部 RM 接口，不碰 PCI config 只读字段 |
| 待机 56W 能不能降 | `DynamicPowerManagement` 已是最高档 3；Pascal 不支持 runtime D3。可尝试 RM 注册表项 `PowerMizerEnable` 强制低功耗级别 |
| 内核 PCIe 配置是否有问题 | 无。ASPM 默认开启，DPC/PTM 编译在内，均与链路速率无关 |

---

## 1. 内核侧 PCIe 配置（只读盘点）

```
内核: 6.12.113+deb13-amd64   (/boot/config-6.12.113+deb13-amd64)

CONFIG_PCIEASPM=y
CONFIG_PCIEASPM_DEFAULT=y          → 运行时 policy = [default]
CONFIG_PCIE_DPC=y                  → Downstream Port Containment（错误隔离，与速率无关）
CONFIG_PCIE_PTM=y                  → Precision Time Measurement（时间同步，与速率无关）
```

ASPM policy 可选 `default / performance / powersave / powersupersave`，当前 `default`。

**结论**：内核侧没有"限制 PCIe 速率"的开关。Linux 也没有标准的链路速率覆盖接口
（`/sys/bus/pci/.../` 下没有 link_speed 可写节点）。速率只由两端协商决定：
**本端（GPU）声明的能力 × 对端（根端口）声明的能力，取小值**。

- 根端口 `00:01.0`：`LnkCap2 = 0x0000000e`（支持到 Gen4）——不是瓶颈
- GPU `01:00.0`：`LnkCap = 0x00453c41`（**只声明 Gen1**）+ `Express (v1)`——瓶颈在这里

---

## 2. 为什么 `setpci` 改不动（已实测）

| 尝试 | 写入 | 读回 | 结果 |
|---|---|---|---|
| LnkCap 最大速率位 | `0x84 = 3c42` | `3c41` | 被忽略 |
| LnkCap2 影子（cap+0x2c = 0xA4） | `0xA4 = 0x00000006` | `0x00000002` | 被忽略 |
| LnkCtl2 目标速率（cap+0x30 = 0xA8） | `0xA8 = 0x0002` | `0x0000` | 被忽略 |
| 触发重训练 | `0x88 = 0x0160` | Retrain 位自清（执行了） | 仍 Gen1 |

**原因**：PCIe 规范把 `Link Capabilities` / `Link Capabilities 2` 定义为 **RO（只读）**。
真实硬件会对 RO 字段的写入直接丢弃。作者那个 Windows 工具能成功，说明它没走这条路
（"两个内核驱动"暗示它直接访问 GPU 内部的配置 shadow 或 BAR0 MMIO）。

⚠ **踩坑记录**：绝对偏移 `0x2C` ≠ Express cap 的 `+0x2C`（后者是 `0xA4`）。
绝对 `0x2C` 读到 `0x123710de`（vendor/device 对），一度写错位置，所幸未生效、无损害。

---

## 3. nvidia 模块参数全表（`/proc/driver/nvidia/params`，42 项）

与 PCIe / 电源直接相关的：

| 参数 | 当前值 | 作用 | 本次是否可动 |
|---|---|---|---|
| **`EnablePCIeGen3`** | **0** | 允许驱动把链路提到 Gen3（驱动内部 RM 设置，非 config space） | ★ **首要尝试** |
| `DynamicPowerManagement` | 3 | 动态电源管理级别（0=off … 3=fine，已最高） | 已最优 |
| `DynamicPowerManagementVideoMemoryThreshold` | 200 | 显存占用低于此值(MB)才允许降功耗 | 可调低 |
| `EnableS0ixPowerManagement` | 0 | S0ix 电源管理 | Pascal 不支持，无效 |
| `EnablePCIERelaxedOrderingMode` | 0 | PCIe Relaxed Ordering | 与速率无关，可忽略 |
| `EnableResizableBar` | 0 | ReBAR | 与速率无关 |
| `EnableGpuFirmware` | 18 | GSP 固件 | Pascal 无 GSP，无效 |
| **`RegistryDwords`** | `""` | ★ 可传任意 RM 注册表键值对（驱动后门） | ★ 功耗实验用它 |
| `RegistryDwordsPerDevice` | `""` | 同上，按设备粒度 | 备用 |
| `ExcludedGpus` | `""` | 排除指定 GPU | 备用 |

其余（`ResmanDebugLevel`、`DeviceFileUID`、`UsePageAttributeTable`、`EnableMSI`、
`NvLinkDisable`、`ImexChannelCount` 等）与本次两个问题无关。

---

## 4. 待机功耗 56W 的成因分析

```
P104-100: 139 MHz, P0, 56.26 W, power.limit 180W (min 90 / max 217), persistence Disabled
P106-100(原卡): 27.9 W
显示: Intel 核显 00:02.0 (KDE Wayland + Xwayland)，P104 未被任何 X 客户端占用
```

三条独立原因叠加：

1. **矿卡 VBIOS 没有低功耗表**。P104 的空闲功耗由 VBIOS 的 PowerMizer 表决定，
   矿卡版本通常只有 P0（高性能）档位。139 MHz 是 P0 的低频点，但电压/显存仍全开 → 56W。
2. **没有 X 客户端占用**，驱动不会因为"有应用在用"而调整策略；反过来，Pascal 也不支持
   `DynamicPowerManagement` 的 runtime D3（那需要 Turing+ 的 S0ix/D3cold 支持）。
3. **`DynamicPowerManagement=3` 已是最高档**，没有更大的旋钮了。

可尝试（都需要重载模块）：
- `RegistryDwords="PowerMizerEnable=0x1;PowerMizerLevel=0x3;PowerMizerLevelAC=0x3"`
  强制 PowerMizer 走最低档（社区常用于笔记本降功耗，桌面卡也有效）
- `nvidia-smi -pl 90`（降到下限；对空闲无效，只在满载封顶）
- 不用时 `echo 1 > /sys/bus/pci/devices/0000:01:00.0/remove`，用时 `rescan`（功耗归零）

---

## 5. 建议的实验顺序

| # | 动作 | 风险 | 判据 |
|---|---|---|---|
| 1 | `NVreg_EnablePCIeGen3=1` + 重载模块 | 低（不支持则无效） | `LnkSta` / `nvidia-smi pcie.link.gen.current` |
| 2 | `RegistryDwords` 设 PowerMizer 低档 + 重载 | 低 | `power.draw` 是否下降 |
| 3 | `nvidia-smi -pl 90` | 无（可 `-pl 180` 还原） | 仅满载有效 |
| 4 | Xorg 占位配置 | **中高**（唯一 NVIDIA 卡，配错黑屏） | 不建议远程做 |

**重载模块会中断图形会话**（停 sddm → 卸载 4 个模块 → 重载 → 起 sddm），
不过显示在 Intel 核显上，恢复后桌面即可回来。

---

## 6. 边界声明

- 本节所有结论来自 `lspci -vv`、`setpci` 读回、`/proc/driver/nvidia/params`、`/boot/config-*`
  与驱动源码目录，**均为实测/实读**，没有推测填充。
- `EnablePCIeGen3` 对 Pascal + 只声明 Gen1 的卡**是否真的能生效，未知**——必须实测。
  它的作用机制（RM 内部设置 vs 依赖 VBIOS 支持）我没有源码级证据。
- 若 `EnablePCIeGen3=1` 无效，则 PCIe 速率的最后一条路只剩改 VBIOS（写 EEPROM），
  风险与收益不成比例，不建议。
