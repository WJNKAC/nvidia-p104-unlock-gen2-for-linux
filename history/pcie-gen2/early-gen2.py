#!/usr/bin/env python3
"""early-gen2.py —— 驱动未加载时操作 BAR0（必须先恢复 BAR0 + Memory Space）

★ 血泪修正（reports/182）：
  驱动卸载后 Linux 会把 BAR0 寄存器清零、并清掉 Command 的 Memory Space Enable。
  此时 mmap resource0 或 /dev/mem 读 BAR0 全是 0xffffffff ——
  **这不是掉卡，是 MMIO 根本没启用**。
  之前 nomod-hammer.py 报"端点读不到"就是踩了这个，误判成链路崩溃。

  正确做法（照 cmpunlocker 驱动 patch 的语义，probe 阶段 BAR0 是活的）：
    1. 记录 BAR0 物理地址（从 resource 表或 0x10 读）
    2. 卸载驱动后，写回 BAR0
    3. setpci COMMAND 置 bit1(MemSpace) | bit2(BusMaster)
    4. 然后才读写 0x8841C / 0x8C040 等

用法：sudo python3 early-gen2.py [--loops=600]
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
R_LNKCAP2, R_CONFIG, R_LNKCFG0, R_CYA, R_PLRATE = 0x880A4, 0x8841C, 0x8C040, 0x8C2C0, 0x8C1C0
GRACE = 20
MODS = ["nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia"]
SYS = f"/sys/bus/pci/devices/{DEV}"

def sh(c, t=180):
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

def get_bar0():
    """从 resource 表取 BAR0 物理地址（比 setpci 可靠，unregister 后仍保留）"""
    try:
        with open(f"{SYS}/resource") as f:
            first = f.readline().split()
        start = int(first[0], 16)
        return start if start else None
    except Exception:
        return None

def enable_mmio(bar0):
    """恢复 BAR0 + 打开 Memory Space / Bus Master"""
    sh(f"setpci -s {DEV} 0x10.l={bar0:08x}")
    time.sleep(0.2)
    cmd = pci(DEV, "COMMAND") or 0
    sh(f"setpci -s {DEV} COMMAND={(cmd | 0x6):04x}")   # bit1 MemSpace | bit2 BusMaster
    time.sleep(0.3)
    return pci(DEV, "0x10.l"), pci(DEV, "COMMAND")

def open_bar(mb=0x1000000):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, mb, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

def rd(m, o): return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v): struct.pack_into("<I", m, o, v)

def alive(m):
    for _ in range(GRACE):
        if rd(m, R_LNKCAP2) != 0xFFFFFFFF:
            return True
        time.sleep(0.02)
    return False

PHYS = 0

def main():
    global PHYS
    if os.geteuid() != 0:
        print("需要 root"); return 1
    loops = 600
    for a in sys.argv[1:]:
        if a.startswith("--loops="):
            loops = int(a.split("=", 1)[1])

    print("=== 0. 前提：记录 BAR0（驱动还在时）===")
    bar0 = get_bar0()
    print(f"  resource 表 BAR0 = {('N/A' if bar0 is None else hex(bar0))}")
    if not bar0:
        print("  取不到 BAR0，中止"); return 1
    PHYS = bar0

    # 驱动在时验证可读
    try:
        fd, m = open_bar()
        ok = alive(m)
        print(f"  驱动在时 MMIO 可读: {ok}  LINK_CAP2={rd(m,R_LNKCAP2):#06x} "
              f"0x8C040={rd(m,R_LNKCFG0):#010x}" if ok else "  MMIO 不可读")
        m.close(); os.close(fd)
        if not ok:
            print("  驱动在时都读不到，先重启"); return 1
    except Exception as e:
        print(f"  MMIO 异常: {e}"); return 1

    # ---------- 1. 卸载驱动 ----------
    print("\n=== 1. 卸载 nvidia 驱动 ===")
    for mm in MODS:
        sh(f"lsmod | grep -q '^{mm} ' && rmmod {mm} 2>&1 || true")
    time.sleep(2)

    # ---------- 2. ★ 恢复 BAR0 + Memory Space ----------
    print("\n=== 2. ★ 恢复 BAR0 + 打开 Memory Space（血泪修正）===")
    nb, cmd = enable_mmio(bar0)
    print(f"  BAR0 写回 = {('N/A' if nb is None else format(nb,'#010x'))}  "
          f"COMMAND = {('N/A' if cmd is None else format(cmd,'#06x'))}")

    try:
        fd, m = open_bar()
        ok = alive(m)
        print(f"  MMIO 现在可读: {ok}")
        if not ok:
            print("  仍不可读 —— 说明除了 MemSpace 还缺别的条件")
            print(f"    0x880A4={rd(m,R_LNKCAP2):#010x} 0x8C040={rd(m,R_LNKCFG0):#010x}")
            m.close(); os.close(fd)
            sh("modprobe nvidia"); return 1
        print(f"  ✓ 0x880A4={rd(m,R_LNKCAP2):#06x}  0x8C040={rd(m,R_LNKCFG0):#010x}  "
              f"0x8C2C0={rd(m,R_CYA):#010x}  0x8C1C0={rd(m,R_PLRATE):#010x}")
    except Exception as e:
        print(f"  MMIO 异常: {e}"); sh("modprobe nvidia"); return 1

    # ---------- 3. 解锁声明 ----------
    print("\n=== 3. 解锁声明 ===")
    for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
        wr(m, off, val)
    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    g = rd(m, 0x88610)
    wr(m, 0x88610, (g & ~(1 << 12)) | 1)
    wr(m, 0x8872C, 6)
    print(f"  0x8841C → {rd(m,R_CONFIG):#010x}  LINK_CAP2 = {rd(m,R_LNKCAP2):#06x}")
    if not alive(m):
        print("  !! 解锁阶段死机"); m.close(); os.close(fd); sh("modprobe nvidia"); return 2

    # ---------- 4. 清 DIS_G2 + 设 PL_LINK_RATE（看是否可写）----------
    print("\n=== 4. 尝试 0x8C2C0 (DIS_G2) 与 0x8C1C0 (PL_LINK_RATE) ===")
    cya = rd(m, R_CYA)
    wr(m, R_CYA, cya & ~(1 << 2))
    print(f"  0x8C2C0 {cya:#010x} → {rd(m,R_CYA):#010x}  DIS_G2={(rd(m,R_CYA)>>2)&1}")
    pl = rd(m, R_PLRATE)
    wr(m, R_PLRATE, 0x00240036)
    print(f"  0x8C1C0 {pl:#010x} → {rd(m,R_PLRATE):#010x}"
          f"  {'✓ 可写' if rd(m,R_PLRATE) == 0x00240036 else '✗ 写保护'}")

    # ---------- 5. TLS=2 ----------
    print("\n=== 5. 两端 TLS=2（配置空间）===")
    for sel, nm in ((ROOT, "根端口"), (DEV, "端点")):
        cur = pci(sel, "CAP_EXP+0x30.w")
        if cur is not None:
            sh(f"setpci -s {sel} CAP_EXP+0x30.w={(cur & ~0xF) | 2:04x}")
            now = pci(sel, "CAP_EXP+0x30.w")
            print(f"  {nm}: {cur:#06x} → {('N/A' if now is None else format(now,'#06x'))}")

    # ---------- 6. SPEED ----------
    print("\n=== 6. 0x8C040 SPEED=1 ===")
    sp = rd(m, R_LNKCFG0)
    wr(m, R_LNKCFG0, (sp & ~0x000C0000) | (1 << 18))
    survive = alive(m)
    print(f"  {sp:#010x} → {rd(m,R_LNKCFG0):#010x}  存活={survive}")
    m.close(); os.close(fd)
    if not survive:
        print("  !! 写 SPEED 掉卡"); sh("modprobe nvidia"); return 2

    # ---------- 7. 快速循环重训 ----------
    print(f"\n=== 7. 循环重训 {loops} 轮 × 0.05s ===")
    t0 = time.time(); best = 1
    for i in range(1, loops + 1):
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w=0020:0020")
        st = None
        for _ in range(5):                      # ★ 重训瞬间读失败要重试
            st = pci(DEV, "CAP_EXP+0x12.w")
            if st is not None:
                break
            time.sleep(0.02)
        if st is None:
            print(f"  [{i}] 连续 5 次读不到（{time.time()-t0:.1f}s）")
            break
        gen = st & 0xF
        best = max(best, gen)
        if gen >= 2:
            print(f"  ★ 第 {i} 轮成功（{time.time()-t0:.1f}s）LNKSTA={st:#06x} sp={gen}")
            break
        if i % 150 == 0:
            print(f"  [{i}] sp={gen}（{time.time()-t0:.1f}s）")
        time.sleep(0.05)

    st = pci(DEV, "CAP_EXP+0x12.w")
    print(f"\n  最终 sp={('N/A' if st is None else st & 0xF)}  最佳={best}")
    print("  " + sh(f"lspci -vv -s {DEV} 2>/dev/null | grep -E 'LnkSta:'").stdout.strip())
    if best >= 2:
        print("\n  ★ 达成 Gen2（驱动仍卸载）→ 测带宽")
        print("  " + sh("timeout 150 /data/labs/nvidia/pcie-gen2/bwtest2 256 7 2>&1")
              .stdout.strip().replace("\n", "\n  "))
        st = pci(DEV, "CAP_EXP+0x12.w")
        print(f"  测后 sp={('N/A' if st is None else st & 0xF)}")

    print("\n=== 8. 恢复驱动 ===")
    sh("modprobe nvidia"); time.sleep(5)
    print("  smi: " + sh("nvidia-smi -L 2>&1 | head -1").stdout.strip())
    return 0

if __name__ == "__main__":
    sys.exit(main())
