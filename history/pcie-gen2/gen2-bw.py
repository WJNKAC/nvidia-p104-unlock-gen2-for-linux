#!/usr/bin/env python3
"""gen2-bw.py —— 同一个脚本内完成 解锁→SPEED→重训→确认→立刻测带宽

为什么必须同一个脚本：
  历史上每次"确认 Gen2"和"测带宽"之间隔着一条命令的时间（10–60 s），
  而链路在这个窗口里会掉回 Gen1，导致带宽永远是 0.78 GB/s 的 Gen1 数字。
  本脚本把两步压在一起，中间不留时间窗。

时序铁律（reports/179 §4.3）：
  · 写 SPEED 后等 SETTLE 秒才重训（毫秒级立刻重训会打死端点）
  · 只从【根端口】发 Retrain，绝不碰端点 LNKCTL
  · 绝不写 COMMIT

用法：sudo python3 gen2-bw.py
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CAP2, R_CONFIG, R_VSEC, R_LTSSM = 0x880A4, 0x8841C, 0x88610, 0x8872C
R_LNKCFG0 = 0x8C040
GRACE = 20
SETTLE = 3.0
BW = "/data/labs/nvidia/pcie-gen2/bwtest"

def sh(c, t=180):
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

def state(tag):
    e, r = sp(DEV), sp(ROOT)
    lk = None
    try:
        fd, m = bar()
        if alive(m):
            lk = rd(m, R_LNKCFG0)
        m.close(); os.close(fd)
    except Exception:
        pass
    print(f"  [{tag}] 端点 sp={e} | 根端口 sp={r} | 0x8C040="
          f"{'-' if lk is None else format(lk,'#010x')}")
    return e

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    print("=== 0. 起点 ===")
    state("起点")
    if sp(DEV) is None:
        print("设备不应答，先重启"); return 1

    # ---- 1. 基线带宽（Gen1）----
    print("\n=== 1. Gen1 基线带宽 ===")
    print("  " + sh(f"timeout 120 {BW} 256 10 2>&1").stdout.strip().replace("\n", "\n  "))

    # ---- 2. 解锁声明（安全操作）----
    print("\n=== 2. 解锁声明（只写使能位，实测安全）===")
    fd, m = bar()
    if not alive(m):
        print("MMIO 不应答"); m.close(); os.close(fd); return 1
    for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
        wr(m, off, val)
    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    g = rd(m, R_VSEC)
    wr(m, R_VSEC, (g & ~(1 << 12)) | 1)
    wr(m, R_LTSSM, 6)
    print(f"  0x8841C → {rd(m,R_CONFIG):#010x}   LINK_CAP2 = {rd(m,R_CAP2):#06x}")
    if not alive(m):
        print("!! 解锁阶段就死了"); m.close(); os.close(fd); return 2
    m.close(); os.close(fd)

    # ---- 3. 根端口 TLS=2 ----
    print("\n=== 3. 根端口 TLS=2（让两端目标一致）===")
    rp = pci(ROOT, "CAP_EXP+0x30.w")
    if rp is not None and (rp & 0xF) != 2:
        sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rp & ~0xF)|2):04x}")
        print(f"  根端口 LNKCTL2 {rp:#06x} → {pci(ROOT,'CAP_EXP+0x30.w'):#06x}")
    else:
        print(f"  根端口 LNKCTL2 = {'N/A' if rp is None else format(rp,'#06x')}")

    # ---- 4. 写 SPEED=1（唯一危险点）----
    print(f"\n=== 4. 写 0x8C040.SPEED=1（不发 COMMIT），然后等 {SETTLE:.0f}s ===")
    fd, m = bar()
    lk = rd(m, R_LNKCFG0)
    if ((lk >> 18) & 3) != 1:
        wr(m, R_LNKCFG0, (lk & ~0x000C0000) | (1 << 18))
        print(f"  0x8C040 {lk:#010x} → {rd(m,R_LNKCFG0):#010x}")
    else:
        print(f"  0x8C040 = {lk:#010x}（SPEED 已是 1）")
    if not alive(m):
        print("!! 写 SPEED 后端点已死"); m.close(); os.close(fd); return 2
    print(f"  存活确认：LINK_CAP2 = {rd(m,R_CAP2):#06x} ✓")
    m.close(); os.close(fd)
    time.sleep(SETTLE)

    # ---- 5. 只从根端口重训 ----
    print("\n=== 5. 根端口 Retrain（不碰端点）===")
    ok = False
    for i in range(6):
        v = pci(ROOT, "CAP_EXP+0x10.w")
        if v is None:
            print("  根端口读不到"); break
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")
        time.sleep(1.2)
        e = state(f"第{i+1}轮")
        if e is None:
            print("  端点读不到，中止"); return 3
        if e >= 2:
            ok = True; break

    if not ok:
        print("\n未能升到 Gen2"); return 1

    # ---- 6. ★ 立刻测带宽，不留时间窗 ----
    print("\n=== 6. ★ 立刻测带宽（紧接上一步，无间隔）===")
    e0 = sp(DEV)
    t0 = time.time()
    out = sh(f"timeout 150 {BW} 256 10 2>&1").stdout.strip()
    e1 = sp(DEV)
    print(f"  测前 sp={e0}  测后 sp={e1}  耗时 {time.time()-t0:.1f}s")
    print("  " + out.replace("\n", "\n  "))
    print("  " + sh("lspci -vv -s 01:00.0 2>/dev/null | grep LnkSta:").stdout.strip())

    print("\n=== 7. 再测一次（看能否复现）===")
    e0 = sp(DEV)
    out = sh(f"timeout 150 {BW} 256 10 2>&1").stdout.strip()
    e1 = sp(DEV)
    print(f"  测前 sp={e0}  测后 sp={e1}")
    print("  " + out.replace("\n", "\n  "))
    return 0

if __name__ == "__main__":
    sys.exit(main())
