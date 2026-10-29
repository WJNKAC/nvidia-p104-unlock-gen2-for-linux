#!/usr/bin/env python3
"""retrain-only.py —— 绕开 0x8C040，只靠"声明 + TLS + 根端口重训"升 Gen2

核心思路
--------
前几轮反复被 0x8C040 咬（写 SPEED 有概率把端点弄死，写 COMMIT 必死）。
但 PCIe 标准协商其实不需要它：
    协商速率 = min(两端 LNKCAP 声明, 两端 LNKCTL2 的 TLS 目标)
我们已能可靠地把声明推到 Gen2（LCAP2 0x02→0x06，纯写 0x8841C/0x88610/0x8872C）。
那么只要：
    ① 声明 = Gen2（LCAP2 = 0x06）
    ② 两端 TLS 目标 = 2（5.0 GT/s）
    ③ 从【根端口】发一次 Retrain Link
就该协商出 Gen2，**全程不碰 0x8C040**。

如果成立 ⇒ 掉卡风险归零，整个方案简化成 4 步纯标准 PCIe 操作。

用法：sudo python3 retrain-only.py
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CAP2, R_CONFIG, R_VSEC, R_LTSSM = 0x880A4, 0x8841C, 0x88610, 0x8872C
R_LNKCFG0, R_LNKCTL2, R_LNKSTAT = 0x8C040, 0x880A8, 0x88088
GRACE = 20

def sh(c, t=60):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(c, 124, "", "TIMEOUT")

def rd(m, o): return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v): struct.pack_into("<I", m, o, v)

def bar():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

def alive(m):
    for _ in range(GRACE):
        if rd(m, R_CAP2) != 0xFFFFFFFF:
            return True
        time.sleep(0.02)
    return False

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None

def sp(sel):
    v = pci(sel, "CAP_EXP+0x12.w")
    return None if v is None else (v & 0xF)

def wr_pci(sel, reg, val, width):
    fmt = "04x" if width == "w" else "08x"
    return sh(f"setpci -s {sel} {reg}.{width}={val:{fmt}}")

def report(tag):
    e, r = sp(DEV), sp(ROOT)
    l2 = pci(DEV, "CAP_EXP+0x2c.l")
    print(f"  [{tag}] 端点 sp={e} | 根端口 sp={r} | LCAP2="
          f"{'-' if l2 is None else format(l2,'#06x')}")
    return e, r, l2

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    print("=== 前提检查 ===")
    report("起点")
    if sp(DEV) is None:
        print("设备不应答，先重启"); return 1

    fd, m = bar()
    if not alive(m):
        print("MMIO 不应答，先重启"); m.close(); os.close(fd); return 1

    # ---- 第1步：只写使能位（这一步已实测安全，从不掉卡）----
    print("\n=== 第1步：解锁声明（不碰 0x8C040）===")
    for off, val, name in [(0x8E110, 1, "XP3G_OVR0"), (0x8E11C, 4, "XP3G_OVR3"),
                           (0x8E120, 0, "XP3G_VAL0"), (0x8E12C, 0x200000, "XP3G_VAL3")]:
        wr(m, off, val)
        print(f"  {name:10s} = {val:#010x}  {'✓' if rd(m, off) == val else '✗'}")

    # ★ 必须读当前值再 OR，绝不能写绝对值（写绝对值会掉卡，reports/180）
    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    print(f"  0x8841C    = {c:#010x} → {rd(m, R_CONFIG):#010x}  (置15/13/11,清12/14)")

    g = rd(m, R_VSEC)
    wr(m, R_VSEC, (g & ~(1 << 12)) | 1)
    print(f"  0x88610    = {g:#010x} → {rd(m, R_VSEC):#010x}  (清12,置0)")

    wr(m, R_LTSSM, 6)
    print(f"  0x8872C    = {rd(m, R_LTSSM):#x}")

    if not alive(m):
        print("!! 第1步就死了（理论上不该发生）"); m.close(); os.close(fd); return 2
    print(f"  安全网：LINK_CAP2 = {rd(m, R_CAP2):#06x} ✓")

    # ---- 第2步：两端 TLS=2 ----
    print("\n=== 第2步：两端 TLS 目标 = 2（5.0 GT/s）===")
    ep = pci(DEV, "CAP_EXP+0x30.w")
    if ep is not None and (ep & 0xF) != 2:
        wr_pci(DEV, "CAP_EXP+0x30", (ep & ~0xF) | 2, "w")
        print(f"  端点   LNKCTL2 {ep:#06x} → {pci(DEV,'CAP_EXP+0x30.w'):#06x}")
    else:
        print(f"  端点   LNKCTL2 = {'N/A' if ep is None else format(ep,'#06x')}（已是 2）")
    rp = pci(ROOT, "CAP_EXP+0x30.w")
    if rp is not None and (rp & 0xF) != 2:
        wr_pci(ROOT, "CAP_EXP+0x30", (rp & ~0xF) | 2, "w")
        print(f"  根端口 LNKCTL2 {rp:#06x} → {pci(ROOT,'CAP_EXP+0x30.w'):#06x}")
    else:
        print(f"  根端口 LNKCTL2 = {'N/A' if rp is None else format(rp,'#06x')}（已是 2）")
    m.close(); os.close(fd)

    # ---- 第3步：只从根端口重训 ----
    print("\n=== 第3步：从【根端口】发 Retrain Link（不碰端点、不碰 0x8C040）===")
    for i in range(6):
        v = pci(ROOT, "CAP_EXP+0x10.w")
        if v is None:
            print(f"  第{i+1}轮: 根端口读不到"); break
        wr_pci(ROOT, "CAP_EXP+0x10", v | 0x20, "w")
        time.sleep(1.0)
        e, r, l2 = report(f"第{i+1}轮")
        if e is not None and e >= 2:
            print(f"\n★★★ Gen2 达成，且全程未写 0x8C040 ★★★")
            print(sh("lspci -vv -s 01:00.0 2>/dev/null | grep -E 'LnkSta:'").stdout.strip())
            print("\n带宽：")
            print(sh("timeout 120 /data/labs/nvidia/pcie-gen2/bwtest 256 10 2>&1").stdout.strip())
            return 0
        if e is None:
            print("  端点读不到了，中止"); return 3
    print("\n未能升到 Gen2")
    report("结束")
    return 1

if __name__ == "__main__":
    sys.exit(main())
