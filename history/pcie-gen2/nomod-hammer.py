#!/usr/bin/env python3
"""nomod-hammer.py —— 驱动卸载状态下做 cmpunlocker 式快速循环重训

要验证的问题（用户的方案）：
  "你之前所有的用户态 setpci 尝试之所以失败，很可能是因为没有在早期启动阶段
   抢在 RM 之前完成重训"
  ⇒ 那么：把 nvidia 驱动整个卸掉（RM 完全不在），再做 600 轮 × 0.05s 的
    连续重训，能不能升到 Gen2？

同时验证一个更根本的问题：
  配置空间 LNKCTL2 的 TLS 写 2，是否会被硬件钳回 1？
  · 如果驱动不在时仍被钳回 1  ⇒ 硬件真实 capability 就是 Gen1，
    我们改的 LCAP2 只是镜像 ⇒ 用户态无解，必须 SEC2 booter
  · 如果不再被钳            ⇒ 是 RM 在压制，行为早期启动可以抢

用法：sudo python3 nomod-hammer.py [--loops=600]
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CONFIG, R_LNKCFG0, R_LNKCAP2 = 0x8841C, 0x8C040, 0x880A4
GRACE = 20
MODS = ["nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia"]

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
        if rd(m, R_LNKCAP2) != 0xFFFFFFFF:
            return True
        time.sleep(0.02)
    return False

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None

def mods_loaded():
    r = sh("lsmod | grep -c '^nvidia'").stdout.strip()
    return r not in ("", "0")

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    loops = 600
    for a in sys.argv[1:]:
        if a.startswith("--loops="):
            loops = int(a.split("=", 1)[1])

    print("=== 0. 起点 ===")
    print(f"  驱动已加载: {mods_loaded()}")
    print(f"  端点 LNKCTL2 = {(v:=pci(DEV,'CAP_EXP+0x30.w')) and format(v,'#06x')}")

    # ---------- 1. 卸掉驱动 ----------
    print("\n=== 1. 卸载 nvidia 驱动（RM 完全不在）===")
    for m in MODS:
        r = sh(f"lsmod | grep -q '^{m} ' && rmmod {m} 2>&1 || true")
        if r.stdout.strip():
            print(f"  rmmod {m}: {r.stdout.strip()}")
    time.sleep(2)
    print(f"  卸载后驱动已加载: {mods_loaded()}")

    fd, m = bar()
    if not alive(m):
        print("  MMIO 不应答，中止"); m.close(); os.close(fd); return 1

    # ---------- 2. 解锁声明 ----------
    print("\n=== 2. 解锁声明（XP3G + 0x8841C + 0x88610 + 0x8872C）===")
    for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
        wr(m, off, val)
    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    g = rd(m, 0x88610)
    wr(m, 0x88610, (g & ~(1 << 12)) | 1)
    wr(m, 0x8872C, 6)
    print(f"  0x8841C → {rd(m,R_CONFIG):#010x}   LINK_CAP2 = {rd(m,R_LNKCAP2):#06x}")
    if not alive(m):
        print("  !! 解锁阶段死机"); m.close(); os.close(fd); return 2
    m.close(); os.close(fd)

    # ---------- 3. ★ 关键测量：TLS 是否还被钳 ----------
    print("\n=== 3. ★ 关键：驱动不在时，TLS=2 还会被钳回 1 吗？ ===")
    before = pci(DEV, "CAP_EXP+0x30.w")
    for sel, nm in ((DEV, "端点"), (ROOT, "根端口")):
        cur = pci(sel, "CAP_EXP+0x30.w")
        if cur is None:
            print(f"  {nm} 读不到"); continue
        sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")
        after = pci(sel, "CAP_EXP+0x30.w")
        tls = (after & 0xF) if after is not None else None
        verdict = ""
        if tls == 2:
            verdict = "  ✓ TLS=2 被接受"
        elif tls == 1:
            verdict = "  ✗ 被硬件钳回 1 ← 硬件真实 capability 仍是 Gen1"
        print(f"  {nm}: 写前 {cur:#06x} → 写 (TLS=2) → 读回 "
              f"{('N/A' if after is None else format(after,'#06x'))}{verdict}")

    # ---------- 4. 快速循环重训（hammer.sh 的节奏）----------
    print(f"\n=== 4. 循环重训 {loops} 轮 × 0.05s（不碰 0x8C040）===")
    t0 = time.time()
    ok = False
    for i in range(1, loops + 1):
        sh(f"setpci -s {ROOT} CAP_EXP+0x30.w=0002:000f")
        sh(f"setpci -s {DEV}  CAP_EXP+0x30.w=0002:000f")
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w=0020:0020")
        st = pci(DEV, "CAP_EXP+0x12.w")
        if st is None:
            print(f"  [{i}] 端点读不到 —— 掉了"); return 3
        gen = st & 0xF
        if gen >= 2:
            print(f"  ★ 第 {i} 轮成功（{time.time()-t0:.1f}s）: LNKSTA={st:#06x} sp={gen}")
            ok = True
            break
        if i % 150 == 0:
            print(f"  [{i}] sp={gen}（{time.time()-t0:.1f}s）")
        time.sleep(0.05)

    if not ok:
        st = pci(DEV, "CAP_EXP+0x12.w")
        print(f"  {loops} 轮后仍未升速，sp={('N/A' if st is None else st & 0xF)}")

    # ---------- 5. 结论 ----------
    print("\n=== 5. 结论 ===")
    tls_now = pci(DEV, "CAP_EXP+0x30.w")
    print(f"  端点 LNKCTL2 = {('N/A' if tls_now is None else format(tls_now,'#06x'))} "
          f"(TLS={('-' if tls_now is None else tls_now & 0xF)})")
    print(f"  链路是否上 Gen2: {ok}")
    if not ok:
        print("\n  ⇒ 驱动不在也无法升速。说明瓶颈不在 RM 压制，")
        print("    而在硬件真实 capability（TLS 被钳回 1）。")
        print("    这正是 cmpunlocker 需要 SEC2 booter 注入的原因 ——")
        print("    只有让 GPU 在 POST 阶段自己重算 capability 才行。")

    # 恢复驱动
    print("\n=== 6. 恢复驱动 ===")
    sh("modprobe nvidia")
    time.sleep(5)
    print("  smi: " + sh("nvidia-smi -L 2>&1 | head -1").stdout.strip())
    return 0

if __name__ == "__main__":
    sys.exit(main())
