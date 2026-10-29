# N187 — 突破：Gen2 达成；元凶锁定为 COMMIT 位

**日期**：2026-10-28 23:58
**对象**：P104-100（10de:1b87），root port `00:01.0`，580.178.04 / kernel 6.1.0-50-amd64
**脚本**：`/data/labs/nvidia/pcie-gen2/gen2-v2.py`
**日志**：`log-v2-nocommit-*.log`、`log-v2-speedonly-*.log`

---

## 0. 三句话结论

1. **`0x8C040` 的 `bit0`(COMMIT) 是纯粹的有害操作** —— 它是本卡唯一的掉卡原因，且会把已经达成的 Gen2 打回 Gen1。
2. **Gen2 只需要一条写**：把 `0x8C040` 的 `SPEED[19:18]` 从 `2` 改成 `1`，**不发 COMMIT**，链路当场上 Gen2。
3. **`remove`/`rescan` 和 `LNKCTL2` 重训都不是必需的** —— 声明解锁在第 1 步的使能位写完就完成了。

---

## 1. A 组：跳过 `0x8C040`（`--mode=nocommit`）

### 起点

```
CAP2=0x00000002  LNKSTA=0x1041  cfg=0xc0b40500  lnkcfg0=0x20085800
端点 LNKSTA=0x1041 | 根端口 LNKSTA=0x5041 TLS2=0x0043
```

### 第 1 步执行完（还没进第 2 步）

```
XP3G_OVR0 = 1 ✓   XP3G_OVR3 = 4 ✓   XP3G_VAL0 = 0 ✓   XP3G_VAL3 = 0x200000 ✓
0x8841C = 0xc0b40500 → 0xc0b4ad00   (置11/13/15,清12/14)
0x88610 = 0x00000000 → 0x00000001   (清12,置0)
0x8872C = 0x6
安全网：LINK_CAP2 = 0x0006 ✓       ← ★ 声明已经解锁了！
0x8C040 —— 跳过
```

**关键**：`LINK_CAP2` 从 `0x0002` 变成 `0x0006`，就发生在写完 `0x8872C` 之后。
**声明解锁根本不需要"整卡复位"。** 参考项目（Windows）之所以要靠 DICS_DISABLE/ENABLE，
是因为 Windows 驱动会把这些位压回去，必须在驱动卸载的窗口期写。Linux 上我们直接写 BAR0 就行。

### 后续

| 阶段 | 结果 |
|---|---|
| 第 2 步 remove/rescan（unbind=True） | 设备 0.5 s 回来，`power_state=D0`，无事故 |
| 声明锁存 | `LCAP2=0x0006` ✓ |
| 第 3 步 两端 TLS=2 | 端点 `LNKCTL2` 本来就是 `0x0002`；根端口 `0x0043 → 0x0042` |
| 第 3 步 重训 3 轮 | 端点始终 `0x1041` sp=1 —— **没升** |
| 第 4 步 按住 10 s | 450 次，始终 sp=1 |
| 最终 smi | `NVIDIA P104-100, 1, 4` ✓ 驱动完全正常 |

**为什么没升**：声明说"我支持 5.0 GT/s"，但 `0x8C040` 的 **SPEED 字段还是 `2`(2.5 GT/s)**。
声明只是能力广告，SPEED 才是速率请求。广告改了、请求没改，链路当然还是 Gen1。

---

## 2. C 组：只改 SPEED、不发 COMMIT（`--mode=speedonly`）——★ 成功

### 起点（A 组之后，设备健康）

```
CAP2=0x00000006  LNKSTA=0x1041  cfg=0xc0b48d00  lnkcfg0=0x20085800
根端口 LNKSTA=0x5041  TLS2=0x0042
```

### 第 1 步执行完

```
0x8C040 = 0x20085800 → 写入 0x20045800  回读 0x20045800   (SPEED=1，无 COMMIT)
[第1步后] CAP2=0x00000006  LNKSTA=0x1042  cfg=0xc0b4ad00  lnkcfg0=0x20045800
          端点 LNKSTA=0x1042 | 根端口 LNKSTA=0xd042
```

**就这一条写，链路当场上双 Gen2：**

| | Gen1（前） | Gen2（后） |
|---|---|---|
| 端点 LNKSTA | `0x1041` | **`0x1042`** sp=2 |
| 根端口 LNKSTA | `0x5041` | **`0xd042`** |
| `0x8C040` | `0x20085800` | `0x20045800` |

- **回读不再是 `0xffffffff`** —— 与 A 组/旧版的关键差别就是没发 COMMIT
- 硬件在 SPEED 字段变化后**自己发起重训**，不需要 COMMIT
- 根端口 TLS 之前已设为 2，所以它能接纳 Gen2

### remove/rescan 后仍然保持

```
[复位后] LNKSTA=0x1042  lnkcfg0=0x20045800
         端点 LNKSTA=0x1042 | 根端口 LNKSTA=0xd042
```

⇒ **`remove` + `rescan` 不会把 Gen2 打掉**，SPEED 请求也能跨重枚举保持。
⇒ 参考项目说的"必须常驻按住否则掉回 Gen1"，在 Linux 这条路径上**至少不适用于 remove/rescan 场景**。

### 第 3 步

```
端点   LNKCTL2 = 0x10002 → 0x10002    （本来就是 TLS=2，bit16 是另一个使能位）
根端口 LNKCTL2 = 0x0042 → 0x0042
第1轮: 端点 LNKSTA=0x1042 sp=2 | 根端口=0xd042   ✓
```

第 1 轮就确认 Gen2。

### 第 4 步 —— 把设备杀了（脚本 bug）

```
=== 第4步：按住 10s（每 20ms）===
  !! MMIO 失效（端点在按住期间死了）
```

**原因**：`step4()` 里写 `0x8C040` 的分支条件写成了

```python
if ARGS["mode"] != "nocommit":          # ← BUG：speedonly 也会进这个分支
    wr(m, R_LNKCFG0, (l & ~0x000C0000) | (1 << 18) | 1)    # 末尾的 1 就是 COMMIT
```

⇒ `speedonly` 模式在按住循环里**每 20 ms 发一次 COMMIT**，几毫秒内就把卡打死。

### 死后状态

```
端点 config = 0xffffffff      端点 LCAP2 = 读不到（capability not found）
端点 power_state = D3cold
根端口 LNKSTA = 0xd041        ← 从 0xd042 掉回 Gen1，设备不再应答
最终 smi: NVIDIA-SMI has failed ...
NVRM: The NVIDIA probe routine failed for 1 device(s).
NVRM: None of the NVIDIA devices were initialized.
```

抢救：次级总线复位 + rescan → 无效；remove + rescan → 设备彻底从总线消失。**只能断电重启。**

---

## 3. 三次实验的因果矩阵

| 实验 | `0x8841C`/`0x88610`/`0x8872C` | SPEED=1 | COMMIT | 声明 `LCAP2` | 链路 | 设备 |
|---|---|---|---|---|---|---|
| 旧 `full`（23:54） | 写 | 写 | **发** | — | 掉卡前未及读 | ☠ 掉卡 |
| A `nocommit` | 写 | 不写 | 不发 | `0x02→0x06` ✓ | Gen1 | ✅ 完好 |
| C `speedonly` | 写 | **写** | 不发 | `0x06` | **Gen2 ✓** | ✅ 完好（第 4 步前） |
| C 的第 4 步 | 写 | 写 | **发** | — | 掉回 Gen1 | ☠ 掉卡 |

**四个格子把因果切得干干净净：**

- 声明解锁 ← 使能位（`0x8841C` bit11/13/15 + `0x88610` bit0 + `0x8872C=6`）
- 链路提速 ← `0x8C040.SPEED[19:18] = 1`
- 掉卡 ← `0x8C040.bit0`(COMMIT)
- `remove`/`rescan` ← 对本流程**无贡献**（既不解锁声明也不提速，但也不破坏）

---

## 4. 为什么参考项目要用 COMMIT，而我们不能

参考项目文档 `03-走过的弯路.md:17` 说：

> 写 `SPEED` 字段 + `COMMIT` 位实测把链路从 Gen1 改成了 **Gen2**

他们的结论是"SPEED + COMMIT 才行"。但我们的 A/C 对照证明：
**在 Linux 上只写 SPEED 就够了，硬件会自己重训。**

差异推测：

1. **Windows 驱动会回压**。参考项目实测"停止按住就掉回 Gen1"，说明 Windows 上 nvlddmkm
   持续把 SPEED 写回 2。他们必须靠 COMMIT 抢在驱动之前强制提交，
   并且靠 PnP 禁用/启用把驱动整个卸掉才有窗口。
2. **Linux 上 580.178.04 不写 `0x8C040`**。所以我们改完它就一直是 `0x20045800`，
   跨 remove/rescan 都保持。
3. COMMIT 在这张卡的固件里可能是"带掉卡风险"的强制复位路径 —— 参考项目自己也记了
   逐位走查 `0x8C040` 会掉卡（`04-硬件事实表.md:302`）。他们能承受，是因为有
   `Gen2PnpFallback` 靠"禁用→启用"把卡拉回来（Windows 的 disable 会真的给卡断电）。
   **Linux 的 remove/rescan 不给卡断电，所以掉了就回不来。**

---

## 5. 下一步（C2 组）

脚本已修 bug（`step4` 按 mode 区分是否发 COMMIT，且写后强制读回校验）。重启后跑：

```bash
sudo python3 gen2-v2.py --mode=speedonly --hold=40
```

要验证三件事：

1. **驱动正常绑定 + Gen2 共存**（上一轮第 4 步前没测 smi，这次要测）
2. **Gen2 能保持多久** —— 按住循环只写 SPEED（不发 COMMIT），看会不会被驱动压回 Gen1
3. **真带宽** —— 用 H2D/D2H 实测是否接近参考项目的 0.78 → 1.55 GB/s（1.99×）

要盯的判据：`lnkcfg0` 若自己从 `0x20045800` 变回 `0x20085800`，就是驱动在回压（这才需要按住）。
