# 580.178.04

NVIDIA 580.178.04 / kernel 6.12.113+deb13-amd64 / Debian 13 / P104-100。
**这是唯一完整验证过的版本组合。**

---

## 一、怎么用（按顺序做完）

### 第 1 步：修补内核模块

```bash
cd drivers/580.178.04
sudo bash minimal.sh gen2 check     # 先看当前状态（只读）
sudo bash minimal.sh gen2 apply     # 打 2 字节补丁
```

也可以顺手把 `whitelist` 一起打上（它解决的是另一个问题，见第三节）：

```bash
sudo bash minimal.sh whitelist check
sudo bash minimal.sh whitelist apply
```

### 第 2 步：重载（gen2 必须，whitelist 要重建）

**gen2**：重载 nvidia 模块，或直接重启（推荐，最干净）

```bash
sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia
sudo modprobe nvidia nvidia_uvm
```

**whitelist**：必须**重建内核模块**，再重载/重启

```bash
cd /usr/src/nvidia-580.178.04
sudo make -j$(nproc)          # 重新链接出 nvidia.ko
```

> ⚠ 重载前先确认没有程序占用 GPU：`fuser -v /dev/nvidia*`
> 有输出就先关掉（浏览器最常见）。否则 `unbind` 会卡死内核，只能重启。

### 第 3 步：把链路推上去（gen2 专用）

补丁只做到"驱动不再降级"，**链路本身还要有人推**。用仓库根目录的工具：

```bash
cd ../../..                        # 回到仓库根
sudo bash bin/push.sh              # insmod gen2kr + unbind/bind 触发推送
```

### 第 4 步：验证

```bash
sudo bash bin/30-verify.sh         # 带宽 + 稳定性
build/bwtest2 512 15 | grep pinned # 应 ~1.56 / 1.71 GB/s
```

- 成功后：`LNKSTA=2`（5GT/s），pinned H2D ≈ 1.56 GB/s
- 失败/想还原：`sudo bash minimal.sh gen2 revert`，再重载/重启

### 一条龙（省事版）

```bash
sudo bash bin/20-install.sh        # 打补丁 + 编译模块 + 推送，全自动
```

---

## 二、提速：帧率与功耗

P1XX / CMP 矿卡除了 PCIe 受限，还有**另一个独立问题**：帧率卡在 ~40fps、功耗上不去。
那是驱动每批次往 pushbuffer 写 `method 0x3998 = 0xF0(240)`（≈218µs 空转/批）造成的。

**本目录不提供那个补丁**（改的是 `libnvidia-glcore.so`，属用户态 GL 库，另一个课题）。
需要的话去这两处：

- **clear-delay 项目**（用户态方案）：<https://github.com/WJNKAC/nvidia-p106-clear-delay>
- **上游分析**（内核白名单思路，`whitelist` 补丁的出处）：
  <https://github.com/dartraiden/NVIDIA-patcher/issues/3>

> ★ **但注意**：本目录的 `whitelist` 补丁能**从源头上解决它** ——
> 打了之后那条延迟命令**根本不生成**（不是"生成了但跳过"），
> 帧时间 52.202 → 3.668 ms，fps 18.98 → 243.78。
> 所以如果你两边都想要，直接打 `whitelist` 就够，不需要那个用户态补丁。

---

## 三、两个补丁各是什么

| 补丁 | 解决 | 目标文件 | 改动 | 验证 |
|---|---|---|---|---|
| `gen2` | PCIe 链路速率（0.78 → 1.56 GB/s） | `nvidia.ko` | 2 字节 | ✅ 353 轮零降级 |
| `whitelist` | 受限 SKU 判定（连带解决帧率/功耗） | `nv-kernel.o_binary` | 10 字节 | ✅ 命令不再生成 |

两者独立，可单独用，也可叠加。

---

## 四、原理（改的是哪几个字节）

### gen2

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

> 文件偏移 = `.text` 节内偏移 + `0x40`（踩过，容易忘）。
> 本版本 `.text` 内 `_nv017105rm` = `0x127d60`，对应文件偏移 `0x127da6`。

md5：`7f6805ed…` → `514de9d4…`

**⚠ 这一步只做了一半** —— 改完驱动不再降级，但链路还没升上去（见第一节第 3 步）。

### whitelist

`nv-kernel.o_binary` 里有一张「受限设备表」，条目形如 `0x0007XXXX`
（低 16 位 = device ID）。本版本表在**文件偏移 `0x5CFDFCC`**，连续 5 个 dword：

| 偏移 | 值 | 设备 |
|---|---|---|
| `+0x00` | `0x00071B07` | P 系列 |
| `+0x04` | `0x00071B87` | **P104-100** |
| `+0x08` | `0x00071BC7` | P 系列 |
| `+0x0C` | `0x00071C07` | P106-100 |
| `+0x10` | `0x00071C09` | P106-090 |

把低 16 位改成 `0xFFFF`（哨兵值，等于把卡从表里抹掉）：

```
07 1B 07 00  →  FF FF 07 00
87 1B 07 00  →  FF FF 07 00
c7 1B 07 00  →  FF FF 07 00
07 1C 07 00  →  FF FF 07 00
09 1C 07 00  →  FF FF 07 00
```

md5（`nv-kernel.o_binary`）：`caa613d3…` → `f3480248…`

**Linux 只有内核一处** —— `libnvidia-glcore.so` 里不含这张表（搜 P1XX 的 ID 串 0 处）。
Windows 才需要在 6+ 个文件里重复替换（每个 UMD 各带一份副本）。

---

## 五、风险

| 风险 | 说明 |
|---|---|
| 改动闭源二进制 | 可能违反 NVIDIA 许可条款 |
| 版本绑定 | 换驱动版本必须重新定位，**不要复用这里的数字** |
| whitelist 改设备身份 | 影响范围比 `gen2` 大，可能影响未验证的代码路径 |
| 驱动升级会覆盖 | 两个补丁都会丢失，需重打 |

**动手前先读** `../../docs/安全纪律.md`。
