# 580.178.04

NVIDIA 580.178.04 / kernel 6.12.113+deb13-amd64 / Debian 13 / P104-100。

**这是唯一完整验证过的版本组合。** 三个补丁都在这套环境上实测通过。

---

## 三个补丁一览

| 补丁 | 目标文件 | 改动 | 验证状态 |
|---|---|---|---|
| `gen2` | `/lib/modules/<kver>/updates/dkms/nvidia.ko` | 2 字节 | ✅ 353 轮零降级，1.56/1.71 GB/s |
| `delay` | `/usr/lib/x86_64-linux-gnu/libnvidia-glcore.so.580.178.04` | 1 字节 | ✅ 52.195→3.434 ms，fps 18.98→259.79 |
| `whitelist` | `/usr/src/nvidia-580.178.04/nvidia/nv-kernel.o_binary` | 10 字节 | ✅ 52.202→3.668 ms，命令不再生成 |

---

## 1. gen2 —— 让驱动不再把 PCIe 速率降回 Gen1

### 改什么

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

### 为什么这样改是安全的

`(mode-2) > 1` 按无符号比较：

| mode | eax | 结果 |
|---|---|---|
| 1 | `0xFFFFFFFF`（下溢） | bail ★ 唯一的行为变化 |
| 2 | 0 | 继续（与原逻辑一致） |
| 3 | 1 | 继续（与原逻辑一致） |
| ≥4 | ≥2 | bail（与原逻辑一致） |

bail 跳到函数唯一的 return，返回 `%r12d`（入口被 `xor %r12d,%r12d` 清零）
⇒ **返回 0 = `CUDA_SUCCESS`**，驱动认为成功，不报错。

### 怎么定位（不用死记偏移）

用 32 字节签名搜索，**全文件唯一**：

```
8d42ff 4156 4155 4989fd 4154 4531e4 53 4883ed40 48897510 89550c 83f802 0f87
```

签名内两个位置要改：
- 索引 `0x02`：`ff` → `fe`
- 索引 `0x1D`：`02` → `01`

> 文件偏移 = `.text` 节内偏移 + `0x40`（踩过，容易忘）。
> 本版本 `.text` 内 `_nv017105rm` = `0x127d60`，对应文件偏移 `0x127da6`。

### md5

```
打前  7f6805ed15f1ae8122aa78e65a5d48a0
打后  514de9d4fdd7c407a088f1baf4b62f04
```

### ⚠ 这一步只做了一半

改完驱动不再降级，但**链路还没升上去**。要有人解锁端点声明 + 写 SPEED + 重训，
那部分由 `kmod/gen2kr.c` 做（`nv_pci_probe` 返回瞬间）。

### 怎么用

```bash
sudo bash minimal.sh gen2 check
sudo bash minimal.sh gen2 apply
sudo bash minimal.sh gen2 revert
```

---

## 2. delay —— 跳过 gr3d 每批次延迟命令

### 改什么

`libnvidia-glcore.so.580.178.04` 里调用 gr3d 延迟命令前的条件跳转。

```asm
0xB51C7B   cmp byte [rax+0x20B], 0    ; "受限 SKU" 标志
0xB51C82   74 2C     je  0xB51CB0     ; 跳过延迟命令
```

改成无条件跳过：

```
0xB51C82   EB 2C     jmp 0xB51CB0     ; 0x74 → 0xEB
```

### 为什么安全

`74 rel8` 和 `EB rel8` **都是 2 字节**，原地替换不破坏指令边界，
后续所有偏移不变。（换成 `E9 rel32` 才会破坏布局，禁止。）

### 背景

580 延续了 525 引入的限制：每批向 pushbuffer 写入 `method 0x3998 = 0xF0(240)`，
240 × 0.91µs ≈ 218µs/批，帧时间随批次数线性增长 ⇒ 帧率卡在 ~40fps。

策略位偏移在 580 是 `+0x20B`（525 是 `+0xC18`）。

### 只改这一处

文件里还有第二处发射点（`0xDE855E`，函数 `0xDE81E0`，1728 字节），
但**实测本负载下不被调用**（入口断点 5 秒 0 命中），补丁后 pushbuffer 里
`method 0x3998` 样本归零。该处结构也不同（无判据、无条件循环发射），不宜盲改。

### md5

```
打前  18f43321d6fcf95524cfb7e12cb221f2
打后  45b06947204cbd586a5d45a463bb69ba
```

### 怎么用

```bash
sudo bash minimal.sh delay check
sudo bash minimal.sh delay apply
sudo bash minimal.sh delay revert
```

改完**不需要重启**，重新启动图形程序即可生效
（已经在跑的程序要退出重开，因为 `.so` 已映射进内存）。

---

## 3. whitelist —— 从内核受限设备表里抹掉矿卡 ID

### 改什么

`nv-kernel.o_binary` 里有一张「受限设备表」，条目形如 `0x0007XXXX`
（低 16 位 = device ID，`0x0007` 前缀推测是家族标记）。

本版本表位置 **文件偏移 `0x5CFDFCC`**，连续 5 个 dword：

| 偏移 | 值 | 设备 |
|---|---|---|
| `+0x00` | `0x00071B07` | P 系列 |
| `+0x04` | `0x00071B87` | **P104-100** |
| `+0x08` | `0x00071BC7` | P 系列 |
| `+0x0C` | `0x00071C07` | P106-100 |
| `+0x10` | `0x00071C09` | P106-090 |

把低 16 位改成 `0xFFFF`（哨兵值，等于把这张卡从表里「抹掉」）：

```
07 1B 07 00  →  FF FF 07 00
87 1B 07 00  →  FF FF 07 00
c7 1B 07 00  →  FF FF 07 00
07 1C 07 00  →  FF FF 07 00
09 1C 07 00  →  FF FF 07 00
```

### 为什么它比 delay 彻底

打了之后 `method 0x3998` **根本不生成**（不是「生成了但跳过」），
说明那个策略位从未被置位，glcore 走的是另一条分支。

### md5

```
打前  caa613d35d8bdfd615cee91e932689b5
```

（这个 md5 是 `nv-kernel.o_binary` 的，不是最终 `nvidia.ko` 的。）

### ⚠ 必须重建 + 重载

改的是 `nv-kernel.o_binary`（编译输入），不是最终模块。改完要：

```bash
cd /usr/src/nvidia-580.178.04
sudo make -j$(nproc)          # 重新链接出 nvidia.ko
# 然后重载驱动（会中断图形会话）
```

`minimal.sh` 只负责改字节，**不代做重建**（重建方式随发行版/安装方式不同）。

### Linux 只有内核一处

`libnvidia-glcore.so` 里**不含**这张表（搜 P1XX 的 ID 串 → 0 处）。
Windows 才需要在 6+ 个文件里重复替换（每个 UMD 各带一份副本）。

---

## 推荐组合

| 你想要 | 用什么 |
|---|---|
| 只解决 PCIe 带宽 | `gen2` + `gen2kr.ko` 推送 |
| 只解决帧率/功耗，图快 | `delay`（改完重开程序即可） |
| 只解决帧率/功耗，要彻底 | `whitelist`（需重建内核模块） |
| 全都要 | `gen2` + `whitelist`（别再打 `delay`，冗余） |

---

## 验证数据

### gen2

```
353 轮 CUDA 上下文零降级，Xid 0
pinned H2D 1.563 GB/s / D2H 1.705 GB/s（Gen1 时 0.779 / 0.855）
```

### delay

```
n83b -M 7 -c 400 -n 400:
  map_p50  52.195 ms → 3.434 ms
  fps      18.98 → 259.79
  像素校验  chk 5150 不变（画面没坏）
```

### whitelist

```
map_p50  52.202 ms → 3.668 ms
fps      18.98 → 243.78
method 0x3998  40+ 条 → 0 条
```

---

## 风险

| 风险 | 说明 |
|---|---|
| 改动闭源二进制 | 可能违反 NVIDIA 许可条款 |
| 版本绑定 | 换驱动版本必须重新定位，**不要复用这里的数字** |
| whitelist 改设备身份 | 影响范围比 delay 大，可能影响未验证的代码路径 |
| 驱动升级会覆盖 | 三个补丁全部丢失，需重打 |

**动手前先读** `../../docs/安全纪律.md`。
