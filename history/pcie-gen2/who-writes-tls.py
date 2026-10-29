#!/usr/bin/env python3
"""who-writes-tls.py —— 判定"TLS 被改回 1"是驱动主动写，还是硬件重训失败回滚

这是决定 kprobe 方案是否可行的关键实验。

背景（reports/183）：
  观察：驱动不在时写 LNKCTL2=0x0f02 读回 0x0f02；驱动在时读回 0x0f01。
  两种解释：
    H1  nvidia.ko 主动把 TLS 写回 1  → **kprobe 拦截可行**
    H2  硬件在重训失败后把 TLS 回滚到实际可用的速率 → **kprobe 无效**

判定方法（控制变量）：
  在【驱动不在】的前提下：
    A. 写 TLS=2 → 读回（应得 2）
    B. **不写 TLS**，只触发一次根端口 Retrain → 读回
       · 若变 1  ⇒ 硬件回滚（H2 成立，kprobe 无用）
       · 若仍 2  ⇒ 说明之前的"变回 1"是驱动写的（H1 成立，kprobe 有价值）
    C. 再写 TLS=2 → 读回（确认可写性没坏）

用法：sudo python3 who-writes-tls.py
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
R_LNKCAP2 = 0x880A4
GRACE = 20
MODS = ["nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia"]
SYS = f"/sys/bus/pci/devices/{DEV}"

def sh(c, t=120):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(c, 124, "", "TIMEOUT")

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None

def tls(sel=DEV):
    v = pci(sel, "CAP_EXP+0x30.w")
    return (None, None) if v is None else (v, v & 0xF)

def get_bar0():
    try:
        with open(f"{SYS}/resource") as f:
            return int(f.readline().split()[0], 16)
    except Exception:
        return None

def enable_mmio(bar0):
    sh(f"setpci -s {DEV} 0x10.l={bar0:08x}")
    time.sleep(0.2)
    cmd = pci(DEV, "COMMAND") or 0
    sh(f"setpci -s {DEV} COMMAND.w={(cmd | 0x6):04x}")
    time.sleep(0.3)

def mmio_ok(bar0):
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        m = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=bar0)
        ok = False
        for _ in range(GRACE):
            if struct.unpack_from("<I", m, R_LNKCAP2)[0] != 0xFFFFFFFF:
                ok = True; break
            time.sleep(0.02)
        m.close(); os.close(fd)
        return ok
    except Exception:
        return False

def set_tls(sel, val):
    cur = pci(sel, "CAP_EXP+0x30.w")
    if cur is None:
        return None
    sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | val):04x}")
    time.sleep(0.1)
    return pci(sel, "CAP_EXP+0x30.w")

def retrain():
    v = pci(ROOT, "CAP_EXP+0x10.w")
    if v is None:
        return False
    sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")
    return True

def lnksta(sel=DEV, tries=5):
    for _ in range(tries):
        v = pci(sel, "CAP_EXP+0x12.w")
        if v is not None:
            return v
        time.sleep(0.02)
    return None

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1

    print("=== 0. 记录 BAR0（驱动还在时）===")
    bar0 = get_bar0()
    print(f"  BAR0 = {('N/A' if bar0 is None else hex(bar0))}")
    if not bar0:
        print("  取不到 BAR0"); return 1

    # ---------- 1. 卸载驱动 ----------
    print("\n=== 1. 卸载 nvidia 驱动 ===")
    for mm in MODS:
        sh(f"lsmod | grep -q '^{mm} ' && rmmod {mm} 2>&1 || true")
    time.sleep(2)
    loaded = sh("lsmod | grep -c '^nvidia'").stdout.strip()
    print(f"  驱动已加载: {loaded not in ('', '0')}")

    # ---------- 2. 修 BAR0 ----------
    print("\n=== 2. 恢复 BAR0 + MemSpace ===")
    enable_mmio(bar0)
    ok = mmio_ok(bar0)
    print(f"  MMIO 可读: {ok}")
    if not ok:
        print("  BAR0 修不回来，中止"); sh("modprobe nvidia"); return 1

    # ---------- 3. 解锁声明（否则 capability 拦着，测不出真实行为）----------
    print("\n=== 3. 解锁声明（让 capability 镜像说 Gen2）===")
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        m = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=bar0)

        def rd(o): return struct.unpack_from("<I", m, o)[0]
        def wr(o, v): struct.pack_into("<I", m, o, v)

        for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
            wr(off, val)
        c = rd(0x8841C)
        wr(0x8841C, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
        g = rd(0x88610)
        wr(0x88610, (g & ~(1 << 12)) | 1)
        wr(0x8872C, 6)
        print(f"  LINK_CAP2 = {rd(R_LNKCAP2):#06x}")
        m.close(); os.close(fd)
    except Exception as e:
        print(f"  失败: {e}"); sh("modprobe nvidia"); return 1

    # ---------- 4. ★ 实验 A ----------
    print("\n=== 4. 实验 A：写 TLS=2 → 读回 ===")
    v, t = tls()
    print(f"  写前: LNKCTL2={('N/A' if v is None else format(v,'#06x'))} TLS={t}")
    r = set_tls(DEV, 2)
    v, t = tls()
    print(f"  写 2 后: LNKCTL2={('N/A' if v is None else format(v,'#06x'))} TLS={t}  "
          f"{'✓ 保持 2' if t == 2 else '✗ 被改回'}")

    # 同时把根端口也设 2，避免它拽着不放
    rp = pci(ROOT, "CAP_EXP+0x30.w")
    if rp is not None and (rp & 0xF) != 2:
        sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rp & ~0xF) | 2):04x}")
        print(f"  根端口 TLS: {rp:#06x} → {pci(ROOT,'CAP_EXP+0x30.w'):#06x}")

    # ---------- 5. ★ 实验 B：只重训，不写 TLS ----------
    print("\n=== 5. ★ 实验 B：不写 TLS，只触发一次根端口 Retrain ===")
    print("  （这一步决定 kprobe 是否有意义）")
    retrain()
    time.sleep(1.5)
    v, t = tls()
    st = lnksta()
    print(f"  重训后: 端点 LNKCTL2={('N/A' if v is None else format(v,'#06x'))} TLS={t}")
    print(f"          端点 LNKSTA={('N/A' if st is None else format(st,'#06x'))} "
          f"sp={('-' if st is None else st & 0xF)}")
    rv = pci(ROOT, "CAP_EXP+0x30.w")
    print(f"          根端口 LNKCTL2={('N/A' if rv is None else format(rv,'#06x'))} TLS="
          f"{('-' if rv is None else rv & 0xF)}")

    verdict_b = None
    if t is None:
        verdict_b = "读不到（链路可能断了）"
    elif t == 1:
        verdict_b = "★ TLS 被自动改回 1 ⇒ **硬件重训失败回滚**（H2）⇒ kprobe 拦不到"
    elif t == 2:
        verdict_b = "TLS 仍是 2 ⇒ 之前'变回 1'是驱动写的（H1）⇒ kprobe 有价值"
    print(f"\n  ⇒ {verdict_b}")

    # ---------- 6. 实验 C：再试一次写 ----------
    print("\n=== 6. 实验 C：再写一次 TLS=2，确认可写性 ===")
    set_tls(DEV, 2)
    v, t = tls()
    print(f"  写后: TLS={t}  {'✓' if t == 2 else '✗'}")
    st = lnksta()
    print(f"  LNKSTA sp={('-' if st is None else st & 0xF)}")

    # ---------- 7. 复现多轮，看是否稳定 ----------
    print("\n=== 7. 重复 5 轮（每轮：写 TLS=2 → 重训 → 读回）===")
    rollback = 0
    for i in range(5):
        set_tls(DEV, 2)
        retrain()
        time.sleep(1.0)
        v, t = tls()
        st = lnksta()
        print(f"  第{i+1}轮: TLS={t} sp={('-' if st is None else st & 0xF)}")
        if t == 1:
            rollback += 1
        if st is None:
            print("    链路断了，停止"); break
    print(f"\n  回滚次数: {rollback}/5")

    # ---------- 8. 恢复 ----------
    print("\n=== 8. 恢复驱动 ===")
    sh("modprobe nvidia"); time.sleep(5)
    print("  smi: " + sh("nvidia-smi -L 2>&1 | head -1").stdout.strip())
    return 0

if __name__ == "__main__":
    sys.exit(main())
