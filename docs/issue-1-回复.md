# Issue #1 回复 —— 精简版（可直接贴）

---

你说的三层（`LnkCap2` 支持位 → `LnkCtl2` 目标速率 → 重训）是标准协商流程，没错。
但 P104 上实测：**光这三层不够，还差两层**，而且第一步查就能看出来。

**① 前提不成立：我们的卡没声明 5GT/s**

```
LnkCap  = 0x00453c41   MaxSpeed = 1  (2.5GT/s)
LnkCap2 = 0x00000002   bit1 = 0  ← 压根不声明 5.0GT/s
```

不是「声明了 5GT 但停在 2.5GT」，是「声明就没有」。而这两位是**硬件下拉、只读**，
`setpci` 写不进去 —— 得从 NVIDIA 私有 BAR0 绕（`0x8E1xx` + `0x8841C`，
必须读值再 OR 且置复位前位才锁存）。同理，指定速率除了 `LnkCtl2` 还要写私有
`0x8C040.SPEED`（NVIDIA `LINK_CONFIG_0`，标准里没这个寄存器）。

**② 最关键的第三层：驱动会主动降回去**

这条决定 `remove + rescan` 那条路走不通：

```
每次 cuInit → rm_init_adapter+0xcd → _nv017755rm
           → _nv017105rm(mode=1) → 写 0x8C040.SPEED=2 (2.5GT/s)
```

驱动**不是照抄当前速率，是显式要求 Gen1**。perf 断点实测：每次建 CUDA 上下文
都以 `DX=0x1` 进这个函数，一次不落。

所以链路推上 Gen2，第一次 `cuInit` 就被打回；而 `remove + rescan` 会重新 probe，
驱动连声明都重置 —— 推了白推。

**我们的解法**：2 字节补丁让 `set_link_speed(mode=1)` 在入口 bail（返回 0 =
`CUDA_SUCCESS`，驱动无感）+ kretprobe 在 `nv_pci_probe` 返回瞬间做一次推送。
实测 353 轮零降级、pinned 1.56/1.71 GB/s。

**给 P106 的建议** —— 先只读判断，别急写寄存器：

```bash
sudo lspci -vv -s <BDF> | grep -E 'LnkCap:|LnkSta:'
# 2.5GT/s → 和 P104 同一个坑；已经是 5GT/s → 你的方案直接可用
```

③ 最关键的验证：**链路推上去后新建一个 CUDA 上下文，看 `LnkSta` 掉不掉**。
掉 → 就是第 ② 层，得从驱动入手，光改寄存器解决不了。

只读自检可直接跑（不写任何寄存器）：`sudo python3 bin/10-check.sh`

⚠ 一个必踩的坑：`0x8C040.bit0` 是 `COMMIT`，若读值是 `0xffffffff` 时派生写回
（`0xFFFFFFFF & ~0xC0000 | 0x40000 = 0xFFF7FFFF`）会把 bit0 置 1 → 必掉卡。
写回前双校验：`!= 0xffffffff` **且** `bit0 == 0`。

完整调用链见 `docs/原理-驱动为什么降Gen1.md`，踩坑清单见 `docs/安全纪律.md`。
