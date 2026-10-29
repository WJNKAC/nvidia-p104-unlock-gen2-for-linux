#!/usr/bin/env python3
"""hold.py —— 把链路按在 Gen2 上，同时不碰端点

与 gen2-v2.py 的 step4 的关键区别：
  ★ 只做两件事：① 重写 0x8C040.SPEED=1  ② 根端口 LNKCTL bit5 Retrain
  ★ 绝不写端点 LNKCTL 的 Retrain 位 —— 那是实测会打死端点的操作（reports/179）
  ★ 绝不发 COMMIT

用法：sudo python3 hold.py [秒数]
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CAP2 = 0x880A4
R_LNKCFG0 = 0x8C040
GRACE = 20

def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True)

def rd(m, o): return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v): struct.pack_into("<I", m, o, v)

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    return int(s, 16) if s and s != "ffffffff" else None

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

def root_retrain():
    v = pci(ROOT, "CAP_EXP+0x10.w")
    if v is None:
        return
    sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    t0 = time.time(); n = 0; fixes = 0; deaths = 0
    print(f"按住 {dur:.0f}s（每 50ms 检查；只写 SPEED + 根端口重训）")
    while time.time() - t0 < dur:
        try:
            fd, m = bar()
            if not alive(m):
                deaths += 1
                print("  !! BAR0 连续 20 次 0xffffffff")
                m.close(); os.close(fd); return 2
            lk = rd(m, R_LNKCFG0)
            if ((lk >> 18) & 3) != 1:
                wr(m, R_LNKCFG0, (lk & ~0x000C0000) | (1 << 18))
                fixes += 1
            m.close(); os.close(fd)

            ls = pci(DEV, "CAP_EXP+0x12.w")
            if ls is None:
                print("  !! 端点配置读不到"); return 3
            if (ls & 0xF) < 2:
                root_retrain()
            n += 1
        except OSError:
            print("  !! MMIO 映射失败"); return 2

        if n % 100 == 0:
            ls = pci(DEV, "CAP_EXP+0x12.w"); rp = pci(ROOT, "CAP_EXP+0x12.w")
            print(f"  [{time.time()-t0:5.1f}s n={n}] 端点 sp={'-' if ls is None else ls & 0xF} "
                  f"| 根端口={'-' if rp is None else format(rp,'#06x')} | 补写SPEED={fixes}")
        time.sleep(0.05)

    ls = pci(DEV, "CAP_EXP+0x12.w"); rp = pci(ROOT, "CAP_EXP+0x12.w")
    print(f"  结束: 端点 LNKSTA={ls:#06x} sp={ls & 0xF} | 根端口={rp:#06x}")
    print(f"  统计: 采样 {n} 次，补写 SPEED {fixes} 次")
    return 0

if __name__ == "__main__":
    sys.exit(main())
