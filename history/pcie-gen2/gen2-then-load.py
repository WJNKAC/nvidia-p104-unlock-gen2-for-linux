#!/usr/bin/env python3
"""gen2-then-load.py —— ★ 关键顺序实验：先让链路 Gen2，再加载驱动

用户的思路（reports/183）：
  "如果能让 RM 从一开始就按 Gen2 初始化，内部数据面就不会错配。"
  之前我们所有实验都是【驱动已加载(Gen1 下初始化完) → 改链路到 Gen2】
  → 链路层变了但内部数据面没变 → Xid 79。
  本脚本反过来：**先把链路推到 Gen2，再 modprobe**，让 probe 在 Gen2 环境下跑。

同时精确定位"驱动到底改了什么"：probe 前后逐寄存器对比。

步骤：
  1. 记录 BAR0（驱动在时）
  2. 卸载驱动
  3. 恢复 BAR0 + MemSpace
  4. 解锁声明 → SPEED=1 → 两端 TLS=2 → 重训 → 确认 Gen2
  5. ★ 在 Gen2 状态下 modprobe
  6. 对比 probe 前后：LNKCTL2 / SPEED / LNKSTA / LCAP2
  7. 若链路保持 Gen2 → 立刻测带宽（这是真正的新条件）

用法：sudo python3 gen2-then-load.py
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
R_LNKCAP2, R_CONFIG, R_LNKCFG0, R_LNKCTL2 = 0x880A4, 0x8841C, 0x8C040, 0x880A8
GRACE = 20
MODS = ["nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia"]
SYS = f"/sys/bus/pci/devices/{DEV}"
BW = "/data/labs/nvidia/pcie-gen2/bwtest2"

def sh(c, t=200):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(c, 124, "", "TIMEOUT")

def pci(sel, reg, tries=1):
    for _ in range(tries):
        s = sh(f"setpci -s {sel} {reg}").stdout.strip()
        try:
            if s:
                return int(s, 16)
        except ValueError:
            pass
        if tries > 1:
            time.sleep(0.02)
    return None

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

class Mem:
    def __init__(self, bar0):
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, 0x1000000, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=bar0)
    def rd(self, o): return struct.unpack_from("<I", self.m, o)[0]
    def wr(self, o, v): struct.pack_into("<I", self.m, o, v)
    def alive(self):
        for _ in range(GRACE):
            if self.rd(R_LNKCAP2) != 0xFFFFFFFF:
                return True
            time.sleep(0.02)
        return False
    def close(self):
        try: self.m.close()
        except Exception: pass
        try: os.close(self.fd)
        except Exception: pass

def snap(mm, tag):
    """打印关键状态；mm 可为 None（驱动加载后 BAR0 可能变）"""
    ep_tls = pci(DEV, "CAP_EXP+0x30.w")
    rp_tls = pci(ROOT, "CAP_EXP+0x30.w")
    ep_st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
    rp_st = pci(ROOT, "CAP_EXP+0x12.w", tries=5)
    l2 = pci(DEV, "CAP_EXP+0x2c.l")
    line = (f"  [{tag}] 端点 TLS={('-' if ep_tls is None else ep_tls & 0xF)} "
            f"sp={('-' if ep_st is None else ep_st & 0xF)} | "
            f"根端口 TLS={('-' if rp_tls is None else rp_tls & 0xF)} "
            f"sp={('-' if rp_st is None else rp_st & 0xF)} | "
            f"LCAP2={('-' if l2 is None else format(l2,'#06x'))}")
    print(line)
    if mm is not None:
        try:
            if mm.alive():
                print(f"           BAR0: 0x8C040={mm.rd(R_LNKCFG0):#010x}"
                      f"(SPEED={(mm.rd(R_LNKCFG0)>>18)&3}) "
                      f"0x880A8={mm.rd(R_LNKCTL2):#010x}")
        except Exception:
            pass
    return ep_st

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1

    print("=== 0. 起点（驱动已加载）===")
    bar0 = get_bar0()
    print(f"  BAR0 = {('N/A' if bar0 is None else hex(bar0))}")
    if not bar0:
        print("  取不到 BAR0"); return 1
    snap(None, "起点")
    print("  基线带宽：")
    print("  " + sh(f"timeout 150 {BW} 128 5 2>&1").stdout.strip().replace("\n", "\n  "))

    # ---------- 1. 卸载 ----------
    print("\n=== 1. 卸载驱动 ===")
    for mm in MODS:
        sh(f"lsmod | grep -q '^{mm} ' && rmmod {mm} 2>&1 || true")
    time.sleep(2)

    # ---------- 2. 修 BAR0 ----------
    print("\n=== 2. 恢复 BAR0 + MemSpace ===")
    enable_mmio(bar0)
    mem = Mem(bar0)
    if not mem.alive():
        print("  MMIO 不可读，中止"); mem.close(); sh("modprobe nvidia"); return 1
    print("  ✓ 可读")

    # ---------- 3. 推到 Gen2 ----------
    print("\n=== 3. 把链路推到 Gen2（驱动不在）===")
    for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
        mem.wr(off, val)
    c = mem.rd(R_CONFIG)
    mem.wr(R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    g = mem.rd(0x88610)
    mem.wr(0x88610, (g & ~(1 << 12)) | 1)
    mem.wr(0x8872C, 6)
    print(f"  声明: LCAP2 = {mem.rd(R_LNKCAP2):#06x}")

    for sel, nm in ((ROOT, "根端口"), (DEV, "端点")):
        cur = pci(sel, "CAP_EXP+0x30.w")
        if cur is not None:
            sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")
    print(f"  端点 TLS={pci(DEV,'CAP_EXP+0x30.w') & 0xF}  "
          f"根端口 TLS={pci(ROOT,'CAP_EXP+0x30.w') & 0xF}")

    sp = mem.rd(R_LNKCFG0)
    mem.wr(R_LNKCFG0, (sp & ~0x000C0000) | (1 << 18))
    print(f"  0x8C040 {sp:#010x} → {mem.rd(R_LNKCFG0):#010x}  "
          f"存活={mem.alive()}")

    ok = False
    for i in range(1, 201):
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w=0020:0020")
        st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
        if st is not None and (st & 0xF) >= 2:
            print(f"  ★ 第 {i} 轮上 Gen2: LNKSTA={st:#06x}")
            ok = True
            break
        time.sleep(0.05)
    if not ok:
        st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
        print(f"  未升速 sp={('-' if st is None else st & 0xF)}")
        mem.close(); sh("modprobe nvidia"); return 1

    snap(mem, "Gen2 就绪")
    mem.close()

    # ---------- 4. ★ 在 Gen2 状态下加载驱动 ----------
    print("\n=== 4. ★ 在 Gen2 状态下 modprobe（关键顺序）===")
    t0 = time.time()
    r = sh("modprobe nvidia")
    print(f"  modprobe 返回 {r.returncode}  {r.stderr.strip()[:100]}")
    time.sleep(6)
    print(f"  smi: {sh('nvidia-smi -L 2>&1 | head -1').stdout.strip()}")
    print("  NVRM: " + sh("dmesg | grep -i NVRM | tail -3").stdout.strip())

    # ---------- 5. probe 后对比 ----------
    print("\n=== 5. probe 后状态对比（看驱动改了什么）===")
    bar0b = get_bar0() or bar0
    print(f"  BAR0 = {bar0b:#x}")
    try:
        mem2 = Mem(bar0b)
        snap(mem2 if mem2.alive() else None, "probe 后")
        mem2.close()
    except Exception as e:
        print(f"  MMIO 打开失败: {e}")
        snap(None, "probe 后")

    # ---------- 6. 若仍 Gen2，立刻测带宽 ----------
    st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
    if st is not None and (st & 0xF) >= 2:
        print("\n=== 6. ★ 链路仍是 Gen2 → 立刻测带宽（真正的新条件）===")
        out = sh(f"timeout 150 {BW} 128 7 2>&1").stdout.strip()
        print("  " + out.replace("\n", "\n  "))
        st2 = pci(DEV, "CAP_EXP+0x12.w", tries=5)
        print(f"\n  测后 sp={('-' if st2 is None else st2 & 0xF)}  "
              f"{'☠ 掉线' if st2 is None else '✓ 存活'}")
        print("  NVRM: " + sh("dmesg | grep -i NVRM | tail -2").stdout.strip())
    else:
        print("\n=== 6. 链路已被驱动改回 Gen1 —— 驱动确实在 probe 时重置了链路 ===")
        print("  ⇒ kprobe 需要拦截的是 probe 阶段的这个写。")
        # 再试一次：probe 之后再推一次
        print("\n=== 7. probe 之后再推一次（对照组）===")
        try:
            mem3 = Mem(get_bar0() or bar0)
            if mem3.alive():
                c = mem3.rd(R_CONFIG)
                mem3.wr(R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15))
                        & ~((1 << 12) | (1 << 14)))
                g = mem3.rd(0x88610)
                mem3.wr(0x88610, (g & ~(1 << 12)) | 1)
                mem3.wr(0x8872C, 6)
                sp = mem3.rd(R_LNKCFG0)
                mem3.wr(R_LNKCFG0, (sp & ~0x000C0000) | (1 << 18))
                print(f"  LCAP2={mem3.rd(R_LNKCAP2):#06x} 0x8C040={mem3.rd(R_LNKCFG0):#010x}")
                mem3.close()
                for sel in (ROOT, DEV):
                    cur = pci(sel, "CAP_EXP+0x30.w")
                    if cur is not None:
                        sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")
                for i in range(1, 201):
                    sh(f"setpci -s {ROOT} CAP_EXP+0x10.w=0020:0020")
                    st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
                    if st is not None and (st & 0xF) >= 2:
                        print(f"  ★ 第 {i} 轮上 Gen2")
                        out = sh(f"timeout 150 {BW} 128 7 2>&1").stdout.strip()
                        print("  " + out.replace("\n", "\n  "))
                        st2 = pci(DEV, "CAP_EXP+0x12.w", tries=5)
                        print(f"  测后 sp={('-' if st2 is None else st2 & 0xF)}")
                        print("  NVRM: " + sh("dmesg|grep -i NVRM|tail -2").stdout.strip())
                        return 0
                    time.sleep(0.05)
                print("  推不上去")
        except Exception as e:
            print(f"  异常: {e}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
