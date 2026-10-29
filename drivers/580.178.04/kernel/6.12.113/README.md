# kernel / 6.12.113

`gen2kr.ko` 的源码。**这个目录名是「这套模块编译并验证过的内核版本」。**

```
内核     6.12.113+deb13-amd64   (Debian 13 / trixie)
驱动     580.178.04
GPU      P104-100 (10de:1b87)
gcc      14.2.0（与内核构建时一致）
```

---

## 为什么按内核版本分目录

源码用的都是稳定 API（`kretprobe` / `pci_get_device` / `ioremap`），
理论上跨内核版本能编。但**编译产物与内核强绑定**：

- 内核符号表、结构体偏移、ABI 都可能变
- 内核模块必须用**目标机器的内核头**编（`/lib/modules/$(uname -r)/build`）
- 换个内核编出来的 `.ko` 不能拿到另一个内核上加载（`Invalid module format`）

所以「在哪套内核上编过、验过」必须能从目录名一眼看出来。
换内核版本时，新建一个同级目录（如 `6.13.1`），**不要原地改这个目录名** ——
老目录代表的是历史验证记录。

---

## 文件

| 文件 | 说明 |
|---|---|
| `gen2kr.c` | 全部逻辑：kretprobe + 首次推送 + 守卫（含 COMMIT 防护） |
| `Makefile` | `obj-m := gen2kr.o`，`KDIR` 用 `$(uname -r)` 自动取 |
| `gen2kr.service` | systemd 单元（**可选，未启用**；用户明确要求不装） |

## 它做什么

在 `nv_pci_probe` 上挂 kretprobe，在 probe **返回的瞬间**做三件事：

```
① 解锁端点声明
   0x8E110/0x8E11C/0x8E120/0x8E12C  →  XP3G 槽
   0x8841C  →  (读值 | bit11|13|15) & ~(bit12|14)
   0x88610  →  (读值 & ~bit12) | 1
   0x8C2C0  →  清 bit2 (DIS_G2)
   0x8872C = 6  →  触发训练，LNKCAP2 当场从 0x02 变 0x06

② 写 SPEED=1（5.0GT/s）
   0x8C040 的 SPEED[19:18] = 1，★ 绝不写 bit0(COMMIT)

③ 两端 TLS=2，从根端口 Retrain
```

**为什么必须在 probe 返回的瞬间**：驱动在 probe 里会把这些位重置，
早写会被覆盖；晚写则已经错过了链路训练窗口。kretprobe 是这个时机唯一可靠的抓手。

## 编译

```bash
# 在仓库根目录
bash bin/build.sh

# 或手动（★ 不要用 sudo，产物会被 root 拥有）
cd drivers/580.178.04/kernel/6.12.113
make -C /lib/modules/$(uname -r)/build M=$PWD modules
```

`bin/build.sh` 会自动找目录（优先匹配当前内核），编完把 `.ko` 拷到 `build/`。

## 可调参数（insmod 后可在 /sys/module/gen2kr/parameters/ 看）

| 参数 | 默认 | 说明 |
|---|---|---|
| `guard_period_ms` | 200 | 守卫周期（原 100ms） |
| `guard_rounds` | 0 | 守卫轮数，0 = 常驻；>0 只守这么多轮 |
| `guard_enabled` | 1 | 总开关，0 = 守卫完全不动作 |
| `debounce_cnt` | — | 连续几次检测到降级才动手 |
| `retrain_min_ms` | — | 两次重训的最小间隔 |
| `speed_settle_ms` | — | 写 SPEED 后的稳定等待 |
| `stage_gap_ms` | — | stage0(写 0x8872C) → stage1(根端口 Retrain) 的间隔 |

> `guard_enabled=0` **停不掉**"按回 SPEED"那条路（守卫会继续把 SPEED 写回去），
> 要完全停守卫只能 `rmmod`。这是踩过的坑。

## 判据纪律（写在这里防止后来人犯错）

- 判 Gen2 只能看 `LNKSTA[3:0] == 2`（BAR0 `0x88088`）
- **`nvidia-smi` 的任何 PCIe 字段都是驱动缓存**，不可信
- `grep 'Speed 5GT/s'` 会误匹配 `2.5GT/s`（踩过 3 次）
- 带宽必须 `512MB × 15`；`128MB × 3` 会落在恢复窗口里只测到 0.78

## 相关项目（不属于本仓库）

矿卡还有另一个独立问题：帧率卡 ~40fps、功耗上不去
（驱动每批次写 `method 0x3998 = 0xF0(240)`，≈218µs 空转）。

本仓库不提供那个补丁，需要的话：

- **clear-delay 项目**（用户态方案）：<https://github.com/WJNKAC/nvidia-p106-clear-delay>
- **上游分析**（内核白名单思路）：<https://github.com/dartraiden/NVIDIA-patcher/issues/3>

> 注：本仓库 `drivers/<版本>/00-whitelist.sh` 那个补丁能**从源头**解决它 ——
> 打了之后延迟命令根本不生成。所以真要解决帧率问题，用 `whitelist` 比用户态补丁更彻底。

---

## ⚠ 动手前

`docs/安全纪律.md` 请先读完。尤其注意：

- **`unbind` 前必须确认没有进程占用 GPU**（`fuser -v /dev/nvidia*`），
  否则会卡死内核、`kill -9` 无效、只能重启（2026-10-29 事故）
- 改 BAR0 链路控制寄存器**必须走内核路径**，用户态 `mmap` 写会打挂设备
