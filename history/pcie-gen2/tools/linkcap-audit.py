#!/usr/bin/env python3
"""linkcap-audit.py —— 只读审计：端点/根端口「声明的链路能力」到底是多少

## 为什么重要（reports/198）

`nvidia-smi -q` 显示：
    Max            : 2
    Current        : 1
    Device Max     : 1     ← 驱动认为端点最大只有 Gen1
    Host Max       : 3

而驱动内部 `_nv017279rm` 把 5 路能力**按位与**后交给 `_nv017812rm`
去置 `obj[0x954]`（cap Gen2）。若这一位为 0，`_nv017105rm`/`_nv017102rm`
（驱动的 `set_link_speed(mode)`）在 mode>=2 时会**直接 bail**。

⇒ 若 `Device Max = 1` 来自"端点声明 LnkCap 仍是 2.5GT/s"，
  那就是整个 Gen2 课题的**架构级根因**：
  硬件声明 Gen1 → 驱动能力位=0 → 永不请求 Gen2 → RM 内部按 Gen1 初始化。

`lspci` 里 `Speed 5GT/s (overdriven)` 的 **overdriven** 标记正是证据：
实际速率 > 声明速率。说明我们改的是"声明镜像"，但 **config space 的 LnkCap 没变**。

本脚本只读，不做任何写操作。输出：
  1. config space 的 LnkCap / LnkCap2 / LnkSta / LnkCtl2（端点 + 根端口）
  2. BAR0 的对应镜像寄存器
  3. 逐字段解码，明确指出"声明"与"实际"的差

用法：sudo python3 linkcap-audit.py
"""
import mmap
import os
import struct
import subprocess
import sys

DEV = "01:00.0"
ROOT = "00:01.0"

# PCIe capability 内偏移
CAP_LNKCAP = 0x0C
CAP_LNKSTA = 0x12
CAP_LNKCTL2 = 0x30
CAP_LNKCAP2 = 0x2C

SPEED_NAME = {1: "2.5GT/s (Gen1)", 2: "5.0GT/s (Gen2)", 3: "8.0GT/s (Gen3)",
              4: "16GT/s (Gen4)", 5: "32GT/s (Gen5)"}

# BAR0 镜像
R_LNKCAP = 0x88084
R_LNKCAP2 = 0x880A4
R_LNKSTA = 0x88088
R_LNKCTL2 = 0x880A8
R_CONFIG = 0x8841C


def setpci(sel, reg):
    r = subprocess.run(f"setpci -s {sel} {reg}", shell=True,
                       capture_output=True, text=True)
    try:
        return int(r.stdout.strip(), 16)
    except Exception:
        return None


def decode_lnkcap2(v):
    """LnkCap2 low 32 bits：byte0 = Supported Link Speeds Vector"""
    if v is None:
        return "读不到"
    vec = v & 0xFF
    out = []
    for gen, bit in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5)):
        if vec & (1 << bit):
            out.append(f"Gen{gen}")
    return f"vector=0x{vec:02x} → " + ("/".join(out) if out else "无")


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1

    print("=" * 68)
    print("1. config space —— 这是内核与驱动真正读到的『声明』")
    print("=" * 68)
    for sel, nm in ((DEV, "端点 01:00.0"), (ROOT, "根端口 00:01.0")):
        lc = setpci(sel, f"CAP_EXP+{CAP_LNKCAP:#04x}.l")
        ls = setpci(sel, f"CAP_EXP+{CAP_LNKSTA:#04x}.w")
        l2 = setpci(sel, f"CAP_EXP+{CAP_LNKCTL2:#04x}.w")
        c2 = setpci(sel, f"CAP_EXP+{CAP_LNKCAP2:#04x}.l")
        print(f"\n【{nm}】")
        if lc is not None:
            msp = lc & 0xF
            print(f"  LnkCap  = 0x{lc:08x}   MaxSpeed={msp} "
                  f"({SPEED_NAME.get(msp, '?')})  Width=x{(lc >> 4) & 0x3F}")
        else:
            print("  LnkCap  读不到")
        if ls is not None:
            csp = ls & 0xF
            print(f"  LnkSta  = 0x{ls:04x}       CurSpeed={csp} "
                  f"({SPEED_NAME.get(csp, '?')})  Width=x{(ls >> 4) & 0x3F}")
        else:
            print("  LnkSta  读不到")
        print(f"  LnkCtl2 = 0x{l2:04x}       TLS={l2 & 0xF} "
              f"({SPEED_NAME.get(l2 & 0xF, '?')})" if l2 is not None
              else "  LnkCtl2 读不到")
        print(f"  LnkCap2 = 0x{c2:08x}   {decode_lnkcap2(c2)}" if c2 is not None
              else "  LnkCap2 读不到")

    print()
    print("=" * 68)
    print("2. BAR0 镜像 —— 我们一直在改的『另一份』")
    print("=" * 68)
    try:
        p = int(open(f"/sys/bus/pci/devices/0000:{DEV}/resource")
                .readline().split()[0], 16)
    except Exception:
        print("取不到 BAR0"); return 2
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    vals = {}
    for off, nm in ((R_LNKCAP, "LNKCAP "), (R_LNKCAP2, "LNKCAP2"),
                    (R_LNKSTA, "LNKSTA "), (R_LNKCTL2, "LNKCTL2"),
                    (R_CONFIG, "0x8841C")):
        v = struct.unpack_from("<I", m, off)[0]
        vals[off] = v
        print(f"  0x{off:05x} {nm} = 0x{v:08x}")
    m.close(); os.close(fd)

    print()
    print("=" * 68)
    print("3. 诊断")
    print("=" * 68)
    ep_cap = setpci(DEV, f"CAP_EXP+{CAP_LNKCAP:#04x}.l")
    ep_sta = setpci(DEV, f"CAP_EXP+{CAP_LNKSTA:#04x}.w")
    ep_cap2 = setpci(DEV, f"CAP_EXP+{CAP_LNKCAP2:#04x}.l")
    mirror_cap2 = vals.get(R_LNKCAP2, 0)

    if ep_cap is not None:
        msp = ep_cap & 0xF
        print(f"  端点 LnkCap.MaxSpeed = {msp} ({SPEED_NAME.get(msp, '?')})")
        print(f"     → 驱动据此得出 nvidia-smi 的 'Device Max'")
    if ep_sta is not None:
        csp = ep_sta & 0xF
        if ep_cap is not None and csp > (ep_cap & 0xF):
            print(f"  ★ LnkSta.CurSpeed({csp}) > LnkCap.MaxSpeed({ep_cap & 0xF})")
            print(f"    这就是 lspci 打 '(overdriven)' 的原因：")
            print(f"    实际跑得比声明的快 ⇒ 声明仍是 Gen1，只有链路被硬推上去了")
    print(f"  端点 LnkCap2（config）= 0x{ep_cap2 if ep_cap2 is not None else 0:08x}"
          f"   BAR0 镜像 0x880A4 = 0x{mirror_cap2:08x}")
    if ep_cap2 is not None and (ep_cap2 & 0xFF) != 0x06 and (mirror_cap2 & 0xFF) == 0x06:
        print("  ★★ 两者不一致！")
        print("     我们只改了 BAR0 镜像（0x880A4 = 0x06，声明支持 5GT），")
        print("     但 config space 的 LnkCap2 仍不含 5GT 位 ⇒ 驱动读到的仍是 Gen1。")
        print("     ⇒ 这解释了 Device Max=1、以及为什么 RM 内部永远按 Gen1 初始化。")

    print()
    print("  下一步（若上面出现 ★★）：")
    print("    1. 确认 config space LnkCap2 是否可写（很可能 RO，由硬连线下发）")
    print("    2. 若 RO，则『让驱动认为 Gen2 可用』只能在内存里改能力位")
    print("       （_nv017812rm 置 obj+0x954 那条路），而不是改声明")
    print("    3. 仍以 gen2kr 的 probe 时机方案为准 —— 它绕过声明，直接让链路跑 5GT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
