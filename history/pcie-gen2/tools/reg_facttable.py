#!/usr/bin/env python3
"""reg_facttable.py —— 从全部历史日志自动生成「寄存器事实表」

目的：把散落在 11 份日志里的读数汇总成一张可查的表，
     每个寄存器给出：出现过的值、对应的状态语境、次数。

这是纯静态工作，不依赖实验机。

输出：
  1. 每个寄存器的值分布
  2. 「状态签名」→ 含义 的映射（由 LCAP2/LNKSTA/SPEED/TLS 组合推断）
"""
import glob
import os
import re
import sys
from collections import Counter, defaultdict

LOGDIR = "/data/labs/nvidia/pcie-gen2"

# BAR0 寄存器：名字 → 正则
BAR0_REGS = {
    "0x88084 LINK_CAP":     re.compile(r"0x88084[^\n]*?=?\s*(0x[0-9a-f]{8})"),
    "0x880A4 LINK_CAP2":    re.compile(r"(?:LINK_CAP2|CAP2)=(0x[0-9a-f]+)"),
    "0x880A8 LNKCTL2镜像":  re.compile(r"0x880A8[^\n]*?=(0x[0-9a-f]{8})"),
    "0x88088 LNKSTA镜像":   re.compile(r"0x88088[^\n]*?=?\s*(0x[0-9a-f]{8})"),
    "0x8841C PRIV_MISC_1":  re.compile(r"(?:0x8841C[^\n]*?=|cfg=)(0x[0-9a-f]{8})"),
    "0x8860C VSEC_DEVICE":  re.compile(r"(?:0x8860C[^\n]*?=|vsecDev=)(0x[0-9a-f]+)"),
    "0x88610 VSEC_HIER":    re.compile(r"(?:0x88610[^\n]*?=|HIER=)(0x[0-9a-f]+)"),
    "0x8872C XVE_OVR":      re.compile(r"0x8872C[^\n]*?=(0x[0-9a-f]+)"),
    "0x8C040 LINK_CFG0":    re.compile(r"(?:0x8C040[^\n]*?=|lnkcfg0=)(0x[0-9a-f]+)"),
    "0x8C080 PPCI_WIDTH":   re.compile(r"0x8C080[^\n]*?=(0x[0-9a-f]+)"),
    "0x8C1C0 PL_LINK_RATE": re.compile(r"0x8C1C0[^\n]*?=(0x[0-9a-f]+)"),
    "0x8C2C0 CYA_0":        re.compile(r"(?:0x8C2C0[^\n]*?=|CYA=)(0x[0-9a-f]+)"),
}

# 配置空间（setpci 的 4 位十六进制）
CFG_REGS = {
    "端点 LNKCTL2":  re.compile(r"端点\s+LNKCTL2\s*=\s*(0x[0-9a-f]{4})"),
    "端点 LNKSTA":   re.compile(r"端点\s+LNKSTA\s*=\s*(0x[0-9a-f]{4})"),
    "根端口 LNKCTL2": re.compile(r"根端口\s+LNKCTL2\s*=\s*(0x[0-9a-f]{4})"),
    "根端口 LNKSTA":  re.compile(r"根端口\s+LNKSTA\s*=\s*(0x[0-9a-f]{4})"),
}

BAR0_VALS = {
    "0x8C040 LINK_CFG0": {"0x20085800": "SPEED=2 (2.5GT/s, Gen1)",
                           "0x20045800": "SPEED=1 (5.0GT/s, Gen2)",
                           "0x200c5800": "SPEED=3 (8.0GT/s, Gen3)",
                           "0x20585800": "SPEED=2 + bit22（驱动置过）",
                           "0x20545800": "SPEED=1 + bit22",
                           "0x20045801": "SPEED=1 + COMMIT ← 会掉卡"},
    "0x880A4 LINK_CAP2": {"0x00000002": "只声明 2.5GT/s（未解锁）",
                           "0x00000006": "声明 2.5+5.0GT/s（已解锁）"},
    "0x8841C PRIV_MISC_1": {"0xc0b40500": "冷启动默认（bit15/13/11 全 0）",
                             "0xc0b4ad00": "我们置了 15/13/11、清 12/14",
                             "0xc0b48d00": "驱动置了 bit15+bit11（上一轮训练痕迹）",
                             "0xffffffff": "端点 MMIO 已死"},
    "0x8C2C0 CYA_0": {"0x068711b3": "DIS_G2(bit2)=0（正常）"},
    "0x8C1C0 PL_LINK_RATE": {"0x00020036": "P104 实测值（cmpunlocker 要 0x00240036，写保护）"},
    "0x8C080 PPCI_WIDTH": {"0x00000404": "x4"},
}


def scan():
    files = sorted(glob.glob(os.path.join(LOGDIR, "log-*.log")))
    stats = defaultdict(Counter)
    for f in files:
        try:
            txt = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for name, rx in BAR0_REGS.items():
            for m in rx.finditer(txt):
                v = m.group(1).lower()
                if len(v) == 10:          # 0x + 8 hex
                    stats[name][v] += 1
        for name, rx in CFG_REGS.items():
            for m in rx.finditer(txt):
                stats[name][m.group(1).lower()] += 1
    return files, stats


def main():
    files, stats = scan()
    print(f"扫描 {len(files)} 份日志\n")
    print("=" * 78)
    print("BAR0 / 配置空间 寄存器值分布")
    print("=" * 78)

    for name in BAR0_REGS:
        if name not in stats:
            continue
        print(f"\n【{name}】")
        for v, n in stats[name].most_common():
            note = BAR0_VALS.get(name, {}).get(v, "")
            print(f"  {v}  ×{n:<4} {note}")

    print("\n" + "=" * 78)
    print("配置空间（PCIe capability）")
    print("=" * 78)
    for name in CFG_REGS:
        if name not in stats:
            continue
        print(f"\n【{name}】")
        for v, n in stats[name].most_common()[:8]:
            low = int(v, 16) & 0xF
            note = ""
            if "LNKSTA" in name:
                note = {1: "2.5GT/s", 2: "5.0GT/s", 3: "8.0GT/s"}.get(low, "")
            elif "LNKCTL2" in name:
                note = {1: "TLS=1 (2.5GT)", 2: "TLS=2 (5.0GT)",
                        3: "TLS=3 (8.0GT)"}.get(low, "")
            print(f"  {v}  ×{n:<4} {note}")

    # ---- 状态签名 ----
    print("\n" + "=" * 78)
    print("综合结论：关键寄存器三元组")
    print("=" * 78)
    print("""
  LCAP2 = 0x02  →  声明未解锁，链路必然 Gen1（写 SPEED 会掉卡，见 reports/186）
  LCAP2 = 0x06  →  声明已解锁，此时才可能通过 SPEED + Retrain 上 Gen2

  0x8C040 SPEED[19:18]（Pascal 编码，与 Ampere 相反）
      2 = 2.5 GT/s   ← 冷启动默认，驱动会维持它
      1 = 5.0 GT/s   ← 目标
      0 = 8.0 GT/s
    bit0 = COMMIT   ← 任何状态下写都必死，永不触碰
    bit22           ← 驱动会置上，无害

  0x8841C
      0xc0b40500  冷启动（bit15=0 → 声明无法锁存）
      0xc0b4ad00  已置 15/13/11（我们要的状态）
      0xc0b48d00  驱动置了 bit15+11（说明已训练过一轮）
""")


if __name__ == "__main__":
    sys.exit(main())
