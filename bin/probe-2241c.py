#!/usr/bin/env python3
"""probe-2241c.py —— 只读验证 skbskhud 报告里提到、我们没碰过的两个寄存器语义

## 背景
`skbskhud/p104-gen2-unlock`（Windows 工具）的技术报告声称：

  · `0x8841C` bit15 = **"复位前置值"**：复位前置 1 → 复位后声明锁存为 Gen2（7/7 无反例）
  · `0x8841C` bit14:13 = 降级门（== 3 强制降回 Gen1）
  · `0x2241C` = `PUNITS.PCI`，**bit7 是 live 的 Gen1/Gen2 选择器**
      写 0 立刻降 Gen1，写 1 立刻回 Gen2，**无需复位**
  · `LNKCAP2` 在 Gen2 稳态应为 `0x06`，`0x2241C` 应为 `0x81`

我们的方案从来没碰过 `0x2241C`，也没搞清 `0x8841C` bit15 的语义 ——
只知道"置了它"，不知道为什么。

## 本脚本做什么
**纯只读。** 打印这些位置在我们卡上的实际值，用来交叉验证他的结论是否成立。
不写任何寄存器（写 `0x2241C` bit7=0 会立刻把链路降到 Gen1，风险不值得冒）。

用法：sudo python3 probe-2241c.py
"""
import mmap
import os
import struct
import sys

DEV_RES = "/sys/bus/pci/devices/0000:01:00.0/resource"

REGS = [
    (0x88084, "LNKCAP ", "最大速率声明 (bits[3:0])"),
    (0x880A4, "LNKCAP2", "支持速率向量 (0x06=Gen1+Gen2 已锁存)"),
    (0x88088, "LNKSTA ", "当前速率 (bits[19:16])"),
    (0x880A8, "LNKCTL2", "目标速率 (bits[3:0]) ← 他说这是「温度计不是开关」"),
    (0x8841C, "CONFIG ", "能力生成器输入：bit15=复位前置, bit14:13=降级门"),
    (0x2241C, "PUNITS.PCI", "★ live Gen1/Gen2 选择器 (bit7) —— 我们没碰过"),
    (0x8872C, "LTSSM  ", "触发训练（写 6）"),
    (0x8C040, "LINKCFG0", "SPEED=bits[19:18] (1=5GT/s, 2=2.5GT/s)"),
    (0x8C2C0, "CYA_0  ", "bit2 = DIS_G2"),
]


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    try:
        p = int(open(DEV_RES).readline().split()[0], 16)
    except Exception as e:
        print(f"取不到 BAR0: {e}"); return 1

    f = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    g = lambda o: struct.unpack_from("<I", m, o)[0]

    print(f"BAR0 = 0x{p:x}\n")
    print("=" * 76)
    print(" 寄存器实际值（只读）")
    print("=" * 76)
    for off, name, desc in REGS:
        v = g(off)
        flag = ""
        if off == 0x88088:
            flag = f" CurSpeed={(v >> 16) & 0xF}"
        elif off == 0x8C040:
            flag = f" SPEED={(v >> 18) & 3}"
        elif off == 0x880A8:
            flag = f" TLS={v & 0xF}"
        print(f"  0x{off:05X}  {name:11s} = 0x{v:08x}{flag}")
        print(f"           {desc}")

    print()
    print("=" * 76)
    print(" 关键位交叉验证（对照 skbskhud 的说法）")
    print("=" * 76)

    cfg = g(0x8841C)
    b15 = (cfg >> 15) & 1
    gate = (cfg >> 13) & 3
    print(f"\n  0x8841C = 0x{cfg:08x}")
    print(f"    bit15 (他称「复位前置值」)      = {b15}")
    print(f"    bit14:13 (他称「降级门」)        = {gate}  "
          f"{'← ==3，按他说的会强制降 Gen1' if gate == 3 else '← 不是 3，不强制降级'}")

    pu = g(0x2241C)
    b7 = (pu >> 7) & 1
    b0 = pu & 1
    print(f"\n  0x2241C = 0x{pu:08x}")
    print(f"    bit7 (他称「live Gen1/Gen2 选择器」) = {b7}   "
          f"{'← 1 = FULL，与当前 Gen2 链路一致' if b7 else '← 0，与他说的 Gen2 稳态(0x81)不符'}")
    print(f"    bit0 (PCIE_VERSION)                 = {b0}   "
          f"{'= v2' if b0 else '= v1'}")

    cap2 = g(0x880A4)
    lnksta = g(0x88088)
    cur = (lnksta >> 16) & 0xF

    print()
    print("=" * 76)
    print(" 判读")
    print("=" * 76)
    ok = True
    if cap2 == 0x06:
        print("  ✓ LNKCAP2 = 0x06 —— 声明已锁存为 Gen2，与他的描述一致")
    else:
        print(f"  · LNKCAP2 = 0x{cap2:04x} —— 当前不是 Gen2 稳态")
        ok = False
    if cur == 2:
        print("  ✓ LNKSTA = 2 —— 链路确实跑在 5.0GT/s")
    else:
        print(f"  · LNKSTA = {cur} —— 链路不在 Gen2")
        ok = False
    if b7 == 1:
        print("  ✓ 0x2241C bit7 = 1 —— 印证他「live 选择器」的说法（Gen2 稳态）")
    else:
        print("  ? 0x2241C bit7 = 0 —— 与他说的 Gen2 稳态不符，值得深究")
    if gate != 3:
        print(f"  ✓ 降级门 = {gate}（非 3）—— 没有处于强制降级状态")
    else:
        print("  ⚠ 降级门 = 3 —— 处于强制降 Gen1 状态，但链路仍是 Gen2，矛盾")

    print()
    print("  说明：本脚本**不做任何写入**。")
    print("  验证 0x2241C bit7 的「写 1 立刻回 Gen2」需要写操作，")
    print("  但那会先把链路降到 Gen1（写 0 才测得出切换），风险与收益不匹配 —— 未做。")

    m.close(); os.close(f)
    return 0 if ok else 0


if __name__ == "__main__":
    sys.exit(main())
