#!/usr/bin/env python3
"""commit-unbind.py —— 关键未测组合：unbind 状态下写 COMMIT

历史上写 0x8C040.bit0(COMMIT) 的每一次都掉卡，但**每一次驱动都绑着**：
  · gen2-recipe.py 首跑（23:54）：驱动绑着写 COMMIT → 死
  · C 组按住循环：驱动绑着每 20ms 写 COMMIT → 几毫秒死
而 E 组/T0 证明 **unbind 状态下写 SPEED 是安全的**。

⇒ 从未在 unbind 状态下试过 COMMIT。而 COMMIT 很可能正是
   "让 GPU 自己发起重训并同步内部数据面" 的那一位（参考项目靠它做到 1.55 GB/s）。
   我们如果一直绕开它，就永远只能协商不能承载流量（Xid 79）。

本脚本串行验证：
  C1  unbind → 解锁声明 → 写 SPEED=1 + COMMIT        → 存活？链路？
  C2  若 C1 活且是 Gen2 → bind 驱动                  → 链路是否保持 Gen2？
  C3  若 C2 保持 Gen2 → 立刻测带宽（pinned/pageable 对照）
  C4  若 C2 掉回 Gen1  → 在驱动绑着时再补一次 COMMIT → 是否还能到 Gen2 且不死？

每一步都有健康检查，一死就停。

用法：sudo python3 commit-unbind.py
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CAP2, R_CONFIG, R_VSEC, R_LTSSM = 0x880A4, 0x8841C, 0x88610, 0x8872C
R_LNKCFG0, R_LNKCTL2, R_WIDTH = 0x8C040, 0x880A8, 0x8C080
GRACE = 20
SYS = f"/sys/bus/pci/devices/{DEV}"
DRV = "/sys/bus/pci/drivers/nvidia"
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

def bound():
    return os.path.exists(f"{SYS}/driver")

def do_bind(flag=True):
    f = f"{DRV}/{'bind' if flag else 'unbind'}"
    r = sh(f"echo {DEV} > {f}")
    return r.returncode == 0 and not r.stderr.strip()

def snap(tag):
    e, rp = sp(DEV), sp(ROOT)
    l2 = pci(DEV, "CAP_EXP+0x2c.l")
    d = None
    try:
        fd, m = bar()
        if alive(m):
            d = (rd(m, R_LNKCFG0), rd(m, R_WIDTH), rd(m, R_CAP2))
        m.close(); os.close(fd)
    except Exception:
        pass
    lk = d[0] if d else None
    print(f"  [{tag}] 端点sp={e} 根端口sp={rp} LCAP2="
          f"{('-' if l2 is None else format(l2,'#06x'))} "
          f"0x8C040={('-' if lk is None else format(lk,'#010x'))}"
          f"{'' if lk is None else f' SPEED={(lk>>18)&3}'} "
          f"WIDTH={('-' if not d else format(d[1],'#06x'))} bound={bound()}")
    return e, rp, l2, lk

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    print("=== 起点 ===")
    if sp(DEV) is None:
        print("设备不应答，先重启"); return 1
    snap("起点")

    # ---------- C1: unbind → 解锁 → SPEED + COMMIT ----------
    print("\n=== C1: unbind → 解锁声明 → 写 SPEED=1 + COMMIT ===")
    if bound():
        print(f"  unbind {'成功' if do_bind(False) else '失败'}")
        time.sleep(1.5)
    print(f"  bound = {bound()}")

    fd, m = bar()
    if not alive(m):
        print("  MMIO 不可读，中止"); m.close(); os.close(fd); return 1
    for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
        wr(m, off, val)
    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    g = rd(m, R_VSEC)
    wr(m, R_VSEC, (g & ~(1 << 12)) | 1)
    wr(m, R_LTSSM, 6)
    print(f"  解锁: 0x8841C={rd(m,R_CONFIG):#010x} LCAP2={rd(m,R_CAP2):#06x}")
    if not alive(m):
        print("  !! 解锁阶段就死了"); m.close(); os.close(fd); return 2

    lk = rd(m, R_LNKCFG0)
    new = (lk & ~0x000C0000) | (1 << 18) | 1          # SPEED=1 + COMMIT
    print(f"  ★ 写 0x8C040: {lk:#010x} → {new:#010x}（SPEED=1 + COMMIT）")
    wr(m, R_LNKCFG0, new)
    if not alive(m):
        print("  !! 写完 COMMIT 端点死了 —— unbind 也救不了，COMMIT 确认是毒药")
        m.close(); os.close(fd); return 3
    print(f"  ✓ 活着。回读 0x8C040 = {rd(m,R_LNKCFG0):#010x}"
          f"  LCAP2 = {rd(m,R_CAP2):#06x}  WIDTH = {rd(m,R_WIDTH):#06x}")
    m.close(); os.close(fd)
    time.sleep(1.0)
    e, rp, l2, lk = snap("C1 写完 COMMIT")

    if e is None:
        print("  端点读不到，中止"); return 3

    # 若还没重训，从根端口补一次（等够时间）
    if e < 2:
        print("\n  链路未变，等 3s 后从根端口补一次 Retrain")
        time.sleep(3)
        v = pci(ROOT, "CAP_EXP+0x10.w")
        if v is not None:
            sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")
        time.sleep(1.5)
        e, rp, l2, lk = snap("根端口重训后")

    if e is None:
        print("  端点掉了，中止"); return 3

    # ---------- C2: bind 驱动，看链路是否保持 ----------
    print("\n=== C2: bind 驱动，看链路是否保持 Gen2 ===")
    ok = do_bind(True)
    print(f"  bind {'成功' if ok else '失败（可能已自动绑定）'}")
    time.sleep(6)
    e2, rp2, l22, lk2 = snap("C2 bind 后")

    if e2 is None:
        print("  !! bind 后端点读不到"); return 3
    print(f"  smi: {sh('nvidia-smi -L 2>&1 | head -1').stdout.strip()}")

    # ---------- C3: 若保持 Gen2，立刻测带宽 ----------
    if e2 >= 2:
        print("\n=== C3: ★ 链路保持 Gen2 且驱动已绑定 → 立刻测带宽 ===")
        out = sh(f"timeout 150 {BW} 256 10 2>&1").stdout.strip()
        print("  " + out.replace("\n", "\n  "))
        e3 = sp(DEV)
        print(f"  测后端点 sp={e3}")
        if e3 is None:
            print("  !! 测带宽后掉卡 —— 仍是 Xid 79 那类问题")
            print("  NVRM: " + sh("dmesg | grep -i NVRM | tail -2").stdout.strip())
        return 0

    # ---------- C4: bind 后掉回 Gen1，再补 COMMIT ----------
    print("\n=== C4: bind 后掉回 Gen1 → 在驱动绑着时再补一次 SPEED+COMMIT ===")
    try:
        fd, m = bar()
        if not alive(m):
            print("  MMIO 不可读"); m.close(); os.close(fd); return 3
        lk = rd(m, R_LNKCFG0)
        new = (lk & ~0x000C0000) | (1 << 18) | 1
        print(f"  写 0x8C040: {lk:#010x} → {new:#010x}")
        wr(m, R_LNKCFG0, new)
        survive = alive(m)
        print(f"  {'✓ 活着' if survive else '!! 死了'} 回读={rd(m,R_LNKCFG0):#010x}")
        m.close(); os.close(fd)
        if not survive:
            return 3
    except Exception as e:
        print(f"  异常: {e}"); return 3
    time.sleep(1.0)
    e4, rp4, _, _ = snap("C4 写完")
    if e4 is not None and e4 < 2:
        v = pci(ROOT, "CAP_EXP+0x10.w")
        if v is not None:
            sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")
        time.sleep(1.5)
        e4, rp4, _, _ = snap("C4 重训后")
    if e4 is not None and e4 >= 2:
        print("\n  ★ Gen2 + 驱动绑定 → 立刻测带宽")
        out = sh(f"timeout 150 {BW} 256 10 2>&1").stdout.strip()
        print("  " + out.replace("\n", "\n  "))
        print(f"  测后 sp={sp(DEV)}")
        print("  NVRM: " + sh("dmesg | grep -i NVRM | tail -2").stdout.strip())
    return 0

if __name__ == "__main__":
    sys.exit(main())
