#!/usr/bin/env python3
"""fight-probe.py —— 在 modprobe 期间高频把 SPEED 写回 1，抢在 RM 之前

依据（reports/183 决定性发现）：
  probe 前后对比证明：nvidia.ko 在 probe 时**只改 0x8C040.SPEED（1→2）**，不碰 TLS。
  Gen2 就绪时 SPEED=1/链路 sp=2 → modprobe 后 SPEED=2/链路 sp=1。

思路：
  不需要 kprobe。在 modprobe 的同时开一个高频循环（每 5–10ms）把 SPEED 写回 1。
  只要循环够快，驱动写完 2 我们就改回 1，链路就有机会保持 Gen2，
  RM 也就在 Gen2 环境下完成内部数据面初始化 —— 这才是避免 Xid 79 的关键。

安全要点：
  · 只写 SPEED 位，**绝不发 COMMIT**、**绝不重训**（毫秒级重训会打死端点）
  · 每次写后回读确认，读到 0xffffffff 立即停（设备已死）
  · BAR0 在 probe 期间可能重映射 → 每次循环重新读 resource 表

用法：sudo python3 fight-probe.py [--interval=0.005] [--hold=8]
"""
import mmap, os, struct, subprocess, sys, threading, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
R_LNKCAP2, R_CONFIG, R_LNKCFG0 = 0x880A4, 0x8841C, 0x8C040
GRACE = 3                     # 循环里少重试，避免拖慢
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

def bar0_addr():
    try:
        with open(f"{SYS}/resource") as f:
            return int(f.readline().split()[0], 16)
    except Exception:
        return None

def enable_mmio(bar0):
    sh(f"setpci -s {DEV} 0x10.l={bar0:08x}")
    time.sleep(0.15)
    cmd = pci(DEV, "COMMAND") or 0
    sh(f"setpci -s {DEV} COMMAND.w={(cmd | 0x6):04x}")

class Bar:
    """可重复打开的 BAR0 映射"""
    def __init__(self, phys):
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, 0x1000000, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=phys)
    def rd(self, o): return struct.unpack_from("<I", self.m, o)[0]
    def wr(self, o, v): struct.pack_into("<I", self.m, o, v)
    def ok(self):
        return self.rd(R_LNKCAP2) != 0xFFFFFFFF
    def close(self):
        try: self.m.close()
        except Exception: pass
        try: os.close(self.fd)
        except Exception: pass

# ---------------- 抢写线程 ----------------
class Fighter(threading.Thread):
    def __init__(self, interval, hold):
        super().__init__(daemon=True)
        self.interval = interval
        self.hold = hold
        self.stop = False
        self.writes = 0
        self.deaths = 0
        self.errors = 0
        self.last_speed = None
        self.dead = False

    def run(self):
        t0 = time.time()
        b = None
        phys = None
        while not self.stop and (time.time() - t0) < self.hold:
            try:
                # BAR0 可能被重新分配，定期重新读
                cur = bar0_addr()
                if cur is None:
                    time.sleep(self.interval); continue
                if b is None or cur != phys:
                    if b: b.close()
                    b = Bar(cur); phys = cur

                v = b.rd(R_LNKCFG0)
                if v == 0xFFFFFFFF:
                    # MMIO 暂不可用（probe 中），不算死
                    time.sleep(self.interval); continue
                self.last_speed = (v >> 18) & 3
                if self.last_speed != 1:
                    b.wr(R_LNKCFG0, (v & ~0x000C0000) | (1 << 18))
                    self.writes += 1
                # 确认活着
                if b.rd(R_LNKCAP2) == 0xFFFFFFFF:
                    self.deaths += 1
                    if self.deaths > 50:
                        self.dead = True
                        break
            except Exception:
                self.errors += 1
                if self.errors > 100:
                    break
            time.sleep(self.interval)
        if b:
            b.close()

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    interval, hold = 0.005, 8.0
    for a in sys.argv[1:]:
        if a.startswith("--interval="): interval = float(a.split("=", 1)[1])
        elif a.startswith("--hold="):   hold = float(a.split("=", 1)[1])

    print("=== 0. 起点 ===")
    bar0 = bar0_addr()
    print(f"  BAR0 = {('N/A' if bar0 is None else hex(bar0))}")
    if not bar0:
        print("  取不到 BAR0"); return 1
    _t, _s = pci(DEV, "CAP_EXP+0x30.w"), pci(DEV, "CAP_EXP+0x12.w", 5)
    print(f"  端点 TLS={'-' if _t is None else _t & 0xF} "
          f"sp={'-' if _s is None else _s & 0xF}")
    if _s is None:
        print("  端点不应答配置读 —— 需先重启"); return 1

    # ---------- 1. 卸驱动 ----------
    print("\n=== 1. 卸载驱动 ===")
    for mm in MODS:
        sh(f"lsmod | grep -q '^{mm} ' && rmmod {mm} 2>&1 || true")
    time.sleep(2)

    # ---------- 2. 修 BAR0 + 推到 Gen2 ----------
    print("\n=== 2. 修 BAR0，把链路推到 Gen2 ===")
    enable_mmio(bar0)
    b = Bar(bar0)
    if not b.ok():
        print("  MMIO 不可读，中止"); b.close(); sh("modprobe nvidia"); return 1
    for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
        b.wr(off, val)
    c = b.rd(R_CONFIG)
    b.wr(R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    g = b.rd(0x88610)
    b.wr(0x88610, (g & ~(1 << 12)) | 1)
    b.wr(0x8872C, 6)
    for sel in (ROOT, DEV):
        cur = pci(sel, "CAP_EXP+0x30.w")
        if cur is not None:
            sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")
    sp = b.rd(R_LNKCFG0)
    b.wr(R_LNKCFG0, (sp & ~0x000C0000) | (1 << 18))
    print(f"  LCAP2={b.rd(R_LNKCAP2):#06x} SPEED={(b.rd(R_LNKCFG0)>>18)&3}")
    # ★ 铁律：写 SPEED 后必须等 ≥3s 才能重训（毫秒级重训会打死端点）
    print("  等待 3s（写 SPEED 后不能立即重训）...")
    time.sleep(3.0)
    if not b.ok():
        print("  !! 等完后 MMIO 已失效"); b.close(); return 2
    for i in range(1, 201):
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w=0020:0020")
        st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
        if st is not None and (st & 0xF) >= 2:
            print(f"  ★ 第 {i} 轮 Gen2：LNKSTA={st:#06x}")
            break
        time.sleep(0.05)
    else:
        print("  未升速"); b.close(); sh("modprobe nvidia"); return 1
    _a, _b = pci(DEV, "CAP_EXP+0x12.w", 5), pci(ROOT, "CAP_EXP+0x12.w", 5)
    print(f"  就绪状态：端点 sp={'-' if _a is None else _a & 0xF} "
          f"根端口 sp={'-' if _b is None else _b & 0xF}")
    b.close()

    # ---------- 3. ★ 抢写 + modprobe ----------
    print(f"\n=== 3. ★ 启动抢写线程（{interval*1000:.0f}ms，{hold:.0f}s）同时 modprobe ===")
    f = Fighter(interval, hold)
    f.start()
    time.sleep(0.3)                     # 让抢写先跑起来
    r = sh("modprobe nvidia")
    print(f"  modprobe 返回 {r.returncode}  {r.stderr.strip()[:100]}")
    time.sleep(6)
    print(f"  smi: {sh('nvidia-smi -L 2>&1 | head -1').stdout.strip()}")
    f.stop = True
    f.join(timeout=3)
    print(f"  抢写统计：写入 {f.writes} 次，last SPEED={f.last_speed}，"
          f"MMIO 失效 {f.deaths} 次，异常 {f.errors} 次，判定死亡={f.dead}")

    # ---------- 4. probe 后状态 ----------
    print("\n=== 4. probe 后状态 ===")
    b2 = None
    try:
        pa = bar0_addr() or bar0
        b2 = Bar(pa)
        if b2.ok():
            print(f"  BAR0={pa:#x} 0x8C040={b2.rd(R_LNKCFG0):#010x}"
                  f"(SPEED={(b2.rd(R_LNKCFG0)>>18)&3}) LCAP2={b2.rd(R_LNKCAP2):#06x}")
        else:
            print(f"  BAR0={pa:#x} MMIO 不可读")
            b2.close(); b2 = None
    except Exception as e:
        print(f"  {e}")
    ep_st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
    rp_st = pci(ROOT, "CAP_EXP+0x12.w", tries=5)
    ep_tls = pci(DEV, "CAP_EXP+0x30.w")
    print(f"  端点 TLS={('-' if ep_tls is None else ep_tls & 0xF)} "
          f"sp={('-' if ep_st is None else ep_st & 0xF)} | "
          f"根端口 sp={('-' if rp_st is None else rp_st & 0xF)}")

    # ---------- 5. 若保住 Gen2，测带宽 ----------
    if ep_st is not None and (ep_st & 0xF) >= 2:
        print("\n=== 5. ★★ 链路保住了 Gen2 且驱动已加载 → 立刻测带宽 ===")
        out = sh(f"timeout 150 {BW} 128 7 2>&1").stdout.strip()
        print("  " + out.replace("\n", "\n  "))
        st2 = pci(DEV, "CAP_EXP+0x12.w", tries=5)
        print(f"\n  测后 sp={('-' if st2 is None else st2 & 0xF)}  "
              f"{'☠ 掉线' if st2 is None else '✓ 存活'}")
        print("  NVRM: " + sh("dmesg | grep -i NVRM | tail -2").stdout.strip())
    else:
        print("\n=== 5. 链路仍是 Gen1 —— 抢写没抢过驱动 ===")
        print("  ⇒ 说明驱动的写在 probe 的很早阶段，或抢写循环不够快/未能及时打开 BAR0")
        print("  ⇒ 此时 kprobe 才是正确的工具（在写指令前改值）")
    if b2:
        b2.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
