# 580.178.04

NVIDIA 580.178.04 / kernel 6.12.113+deb13-amd64 / Debian 13 / P104-100。
**这是唯一完整验证过的版本组合。**

---

## 脚本（按编号顺序执行）

```
00-whitelist.sh    前置，可选    解除「受限 SKU」判定（解决帧率/功耗，与 Gen2 无关）
01-gen2-patch.sh   必做          修补 nvidia.ko，让驱动不再把速率降回 Gen1
02-gen2-push.sh    必做          加载模块并把链路推到 Gen2（start/stop/status）
```

每个脚本都有三种动作：`check`（只读）/ `apply` 或 `start` / `revert` 或 `stop`。

---

## 一、怎么用

### 第 0 步（可选）：解除受限 SKU

**不做也不影响 Gen2**，只影响帧率/功耗。只想提升带宽的话跳过这步。

```bash
sudo bash 00-whitelist.sh check     # 看当前状态
sudo bash 00-whitelist.sh apply     # 打补丁
```

打完后必须**重建内核模块**（脚本会打印完整命令）：

```bash
cd /usr/src/nvidia-580.178.04
sudo make -j$(nproc)
```

然后重载驱动或重启。

### 第 1 步：修补 nvidia.ko

```bash
sudo bash 01-gen2-patch.sh check    # 看当前状态
sudo bash 01-gen2-patch.sh apply    # 打 2 字节补丁
```

打完必须让新代码生效，二选一：

```bash
# A) 重载模块（快，会中断图形会话）
sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia
sudo modprobe nvidia nvidia_uvm

# B) 直接重启（推荐，最干净）
```

> ⚠ **重载/重启前先确认没有程序占用 GPU**：
> ```bash
> fuser -v /dev/nvidia*
> ```
> 有输出就先关掉（浏览器 / Electron 应用最常见）。
> 否则 `unbind` 会卡死内核，进程落入 D 状态、`kill -9` 无效，只能整机重启。

### 第 2 步：把链路推上去

补丁只做到"驱动不再降级"，**链路本身还要有人推**。

```bash
sudo bash 02-gen2-push.sh status    # 先看状态
sudo bash 02-gen2-push.sh start     # 编译（若无）+ 加载模块 + 推送
```

`start` 会依次做：检查补丁 → 准备模块 → **确认 GPU 空闲** → insmod → unbind/bind → 等 Gen2。

### 第 3 步：验证

```bash
sudo bash 02-gen2-push.sh status           # CAP2=0x0006 LNKSTA=2
sudo bash ../../bin/30-verify.sh           # 带宽 + 稳定性
```

- 成功：`LNKSTA=2`（5GT/s），pinned H2D ≈ 1.56 GB/s
- 失败：看 `dmesg | grep gen2kr`

---

## 二、还原（反向依次来）

```bash
sudo bash 02-gen2-push.sh stop      # 先卸模块
sudo bash 01-gen2-patch.sh revert   # 再还原 nvidia.ko
sudo bash 00-whitelist.sh revert    # 最后还原设备表（如果第 0 步做过）
```

每一步之后都要重载驱动或重启（`stop` 除外）。顺序不能反 ——
先 `revert` 补丁而模块还在，守卫会继续把 SPEED 写回去。

---

## 三、02 的完整动作

| 动作 | 说明 |
|---|---|
| `status` | 模块是否加载、模块参数、链路状态（只读） |
| `start` | 编译（若无）+ 加载 + 推送 + 等 Gen2 |
| `stop` | 卸载模块（守卫停止，链路会在下次 `cuInit` 降回 Gen1） |
| `restart` | `stop` + `start` |
| `build` | 只编译模块 |
| `uninstall` | `stop` + 删除编译产物（源码保留） |

---

## 四、提速：帧率与功耗

P1XX / CMP 矿卡除了 PCIe 受限，还有**另一个独立问题**：帧率卡在 ~40fps、功耗上不去。
那是驱动每批次往 pushbuffer 写 `method 0x3998 = 0xF0(240)`（≈218µs 空转/批）造成的。

**本目录不提供那个补丁**（改的是 `libnvidia-glcore.so`，属用户态 GL 库，另一个课题）。
需要的话去这两处：

- **clear-delay 项目**（用户态方案）：<https://github.com/WJNKAC/nvidia-p106-clear-delay>
- **上游分析**（内核白名单思路，`00-whitelist.sh` 的出处）：
  <https://github.com/dartraiden/NVIDIA-patcher/issues/3>

> ★ **但注意**：`00-whitelist.sh` 能**从源头上解决它** ——
> 打了之后那条延迟命令**根本不生成**（不是"生成了但跳过"），
> 帧时间 52.202 → 3.668 ms，fps 18.98 → 243.78。
> 所以如果你两边都想要，直接做第 0 步就够，不需要那个用户态补丁。

---

## 五、目录里的文件

```
00-whitelist.sh      前置（可选）：改 nv-kernel.o_binary
01-gen2-patch.sh     必做：改 nvidia.ko
02-gen2-push.sh      必做：加载模块 + 推送链路
_lib.sh              公共部分（路径、颜色、寄存器读取、GPU 占用检查、模块编译）
_patch.py            通用二进制补丁器（JSON 规格驱动，被 00/01 调用）
offsets.sh           本版本的偏移与原始字节（纯变量，可 source）
kernel/6.12.113/     内核模块源码（按内核版本分目录）
README.md            本文件
```

### 为什么模块按内核版本分目录

补丁偏移跟**驱动版本**绑定；模块编译产物跟**内核版本**绑定。两者是不同的东西：

| | 绑定什么 | 为什么 |
|---|---|---|
| `offsets.sh` / `_patch.py` | 驱动版本 | 改的是驱动二进制的字节偏移 |
| `kernel/<内核版本>/` | 内核版本 | `.ko` 只能用目标机器的内核头编，换内核加载不了 |

换内核版本时**新建同级目录**，不要原地改名 —— 老目录是历史验证记录。

---

## 六、原理（想深究再看）

### 01：nvidia.ko，2 字节

`_nv017105rm`（驱动的 `set_link_speed(mode)`）入口的 mode 白名单
从 `{1,2,3}` 收窄成 `{2,3}`，让 `mode==1` 走原有的 bail 分支。

```asm
_ nv017105rm:
  8d 42 ff              lea    -0x1(%rdx),%eax     ; eax = mode - 1
  ...
  83 f8 02              cmp    $0x2,%eax
  0f 87 xx xx xx xx     ja     <bail>
```

改成：

```asm
  8d 42 fe              lea    -0x2(%rdx),%eax     ; eax = mode - 2   (ff → fe)
  ...
  83 f8 01              cmp    $0x1,%eax                               (02 → 01)
```

`(mode-2) > 1` 按无符号比较：

| mode | eax | 结果 |
|---|---|---|
| 1 | `0xFFFFFFFF`（下溢） | bail ★ 唯一的行为变化 |
| 2 | 0 | 继续（与原逻辑一致） |
| 3 | 1 | 继续（与原逻辑一致） |
| ≥4 | ≥2 | bail（与原逻辑一致） |

bail 跳到函数唯一的 return，返回 `%r12d`（入口被 `xor %r12d,%r12d` 清零）
⇒ **返回 0 = `CUDA_SUCCESS`**，驱动认为成功，不报错。

**怎么定位**（不用死记偏移）——32 字节签名，全文件唯一：

```
8d42ff 4156 4155 4989fd 4154 4531e4 53 4883ed40 48897510 89550c 83f802 0f87
```

签名内两个位置要改：索引 `0x02` 的 `ff`→`fe`，索引 `0x1D` 的 `02`→`01`。

> 文件偏移 = `.text` 节内偏移 + `0x40`。本版本 `.text` 内 `_nv017105rm` = `0x127d60`，
> 对应文件偏移 `0x127da6`。

md5：`7f6805ed…` → `514de9d4…`

### 00：nv-kernel.o_binary，10 字节

里面有一张「受限设备表」，条目形如 `0x0007XXXX`（低 16 位 = device ID）。
本版本表在**文件偏移 `0x5CFDFCC`**，连续 5 个 dword：

| 偏移 | 值 | 设备 |
|---|---|---|
| `+0x00` | `0x00071B07` | P 系列 |
| `+0x04` | `0x00071B87` | **P104-100** |
| `+0x08` | `0x00071BC7` | P 系列 |
| `+0x0C` | `0x00071C07` | P106-100 |
| `+0x10` | `0x00071C09` | P106-090 |

低 16 位改成 `0xFFFF`（哨兵，等于把卡从表里抹掉）：

```
07 1B 07 00  →  FF FF 07 00    87 1B 07 00  →  FF FF 07 00
c7 1B 07 00  →  FF FF 07 00    07 1C 07 00  →  FF FF 07 00
09 1C 07 00  →  FF FF 07 00
```

md5（`nv-kernel.o_binary`）：`caa613d3…` → `f3480248…`

**Linux 只有内核一处** —— `libnvidia-glcore.so` 里不含这张表（搜 P1XX 的 ID 串 0 处）。
Windows 才需要在 6+ 个文件里重复替换（每个 UMD 各带一份副本）。

### 02：模块做什么

在 `nv_pci_probe` 上挂 kretprobe，在 probe **返回的瞬间**：

```
① 解锁端点声明
   0x8E110/0x8E11C/0x8E120/0x8E12C  →  XP3G 槽
   0x8841C  →  (读值 | bit11|13|15) & ~(bit12|14)
   0x88610  →  (读值 & ~bit12) | 1
   0x8C2C0  →  清 bit2 (DIS_G2)
   0x8872C = 6  →  触发训练，LNKCAP2 当场从 0x02 变 0x06

② 写 SPEED=1（5.0GT/s）  0x8C040 的 SPEED[19:18] = 1，★ 绝不写 bit0(COMMIT)

③ 两端 TLS=2，从根端口 Retrain
```

**为什么必须在 probe 返回的瞬间**：驱动在 probe 里会把这些位重置，
早写会被覆盖；晚写则错过了链路训练窗口。kretprobe 是这个时机唯一可靠的抓手。

---

## 七、风险

| 风险 | 说明 |
|---|---|
| 改动闭源二进制 | 可能违反 NVIDIA 许可条款 |
| 版本绑定 | 换驱动版本必须重新定位，**不要复用这里的数字** |
| whitelist 改设备身份 | 影响范围比 `gen2` 大，可能影响未验证的代码路径 |
| 驱动升级会覆盖 | 两个补丁都会丢失，需重打 |

**动手前先读** `../../docs/安全纪律.md`。
