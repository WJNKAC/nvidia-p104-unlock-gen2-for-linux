# Issue #1 回复草稿

> 针对 skbskhud 的「Linux 下原理一样：LnkCap2 支持位 + LnkCtl2 目标速率 + 重训练」

---

你说的三层（`LnkCap2` 支持位 → `LnkCtl2` 目标速率 → 重训）是标准 PCIe 协商流程，没错。
但在 P104 上我们实测下来，**光这三层不够，还差两层**。按你建议的第一步查就能看出来：

## 1. 前提不成立：我们的卡根本没声明 5GT/s

你写的是「**如果声明了 5GT/s 但停在 2.5GT/s**」。P104 原厂是：

```
LnkCap  (0x0C) = 0x00453c41   MaxSpeed = 1  (2.5GT/s)
LnkCap2 (0x2C) = 0x00000002   bit1 = 0  ← 压根不声明 5.0GT/s
LnkSta  (0x12) = 0x1041       CurSpeed = 1
```

所以是「声明就没有」，不是「声明了但没协商上」。而这两个位在 config space 里是
**硬件下拉决定、只读**，`setpci` 写不进去 —— 得从 NVIDIA 私有 BAR0 绕。

## 2. 标准寄存器不够，得动私有寄存器

| 想做的事 | 标准做法 | 我们实际得做的 |
|---|---|---|
| 让端点声明 5GT/s | 写 `LnkCap2` | 只读。要写 BAR0 `0x8E110/0x8E11C/0x8E120/0x8E12C` + `0x8841C`（**必须读值再 OR**，且要置复位前位才会锁存）+ `0x88610`，之后 `LnkCap2` 才变成 `0x06` |
| 指定目标速率 | 写 `LnkCtl2[3:0]=2` | 不够，还要写私有 `0x8C040.SPEED=1`（NVIDIA `LINK_CONFIG_0`，标准里没这个寄存器） |
| 触发重训 | 根端口 `LINKCTL.bit5` | 另外还在写 `0x8872C=6`（私有开关，**驱动自身零引用**） |

顺带：这些写操作在链路动态时**容易掉卡**（我们打挂过多次，`ZOMBIE` / `Xid 79`），
而且必须走**内核路径** —— 用户态 `mmap` 写 BAR0 在链路刚降级时 2/2 必挂。

## 3. 最关键的第三层：驱动会主动降回去

这条是我们花最多时间才挖出来的，也是**为什么 `remove + rescan` 那条路走不通**：

```
每次 cuInit
 └─ rm_init_adapter+0xcd
     └─ _nv017755rm    if (mode == 当前速率) return;
         └─ _nv017105rm(mode=1)   → 写 0x8C040.SPEED=2 (2.5GT/s)
```

`mode=1` 就是 Gen1。驱动**不是照抄当前速率，是显式要求降级**。
perf 断点实测：每次建 CUDA 上下文都以 `DX=0x1` 进这个函数，一次不落。

所以：**你把链路推上 Gen2，第一次 `cuInit` 就被打回去。**
而 `remove + rescan` 会重新 probe，驱动在 probe 里连声明都重置 —— 推了白推。

## 4. 我们最后怎么解的

2 字节补丁 + 一次窗口推送：

- **补丁**：改 `_nv017105rm` 入口的 mode 白名单 `{1,2,3}` → `{2,3}`
  （`lea -0x1` → `-0x2`、`cmp $0x2` → `$0x1`，`mode==1` 下溢走原有 bail，
  返回 0 = `CUDA_SUCCESS`，驱动认为成功、不报错）
  ⇒ 驱动不再降级
- **推送**：`kretprobe` 挂 `nv_pci_probe`，在返回瞬间解锁声明 + 写 SPEED + 重训
  ⇒ 链路升到 Gen2，之后没人再降它

实测：353 轮 CUDA 上下文零降级、Xid 0、pinned H2D 1.56 / D2H 1.71 GB/s。

## 5. 对 P106 的建议

P106-100 是同代 Pascal，RM 布局可能一致，值得试。但**先别急着写寄存器**，
按这个顺序判断，能省掉大部分坑：

```bash
# ① 只读，看声明位（这一步就能决定后面走哪条路）
sudo lspci -vv -s <BDF> | grep -E 'LnkCap:|LnkSta:'

# ② 如果 LnkCap 是 2.5GT/s → 和 P104 同一个坑，标准三层不够
#    如果已经是 5GT/s → 那就只是协商问题，你的方案直接可用
```

③ 最关键的验证：**把链路推上去后，新建一个 CUDA 上下文，看 `LnkSta` 掉不掉**。
掉 → 就是第 3 层（驱动策略），光改寄存器解决不了，得从驱动入手。

仓库里有只读自检脚本，不写任何寄存器，可以直接跑：

```bash
sudo python3 bin/10-check.sh        # 环境 + 寄存器 + 补丁状态，纯只读
sudo python3 bin/peek-regs.py       # 8 个关键寄存器带字段解码
```

⚠ 提醒一句：`0x8C040.bit0` 是 `COMMIT`，**读值若为 `0xffffffff` 时派生写回会把它置 1**
（`0xFFFFFFFF & ~0xC0000 | 0x40000 = 0xFFF7FFFF`），这是必掉卡路径，
我们踩过。写回前务必双校验：`!= 0xffffffff` **且** `bit0 == 0`。

细节都在 https://github.com/WJNKAC/nvidia-p104-unlock-gen2-for-linux
（`docs/原理-驱动为什么降Gen1.md` 有完整调用链，`docs/安全纪律.md` 是踩坑清单）
