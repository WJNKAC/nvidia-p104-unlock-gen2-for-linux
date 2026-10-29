#!/usr/bin/env python3
"""speed-once.py —— 只写一条 0x8C040.SPEED=1，然后看结果

用途：在"其他一切都已就绪"的条件下，单独检验 SPEED 写这一条命令的
可靠性与后果。前面几轮实验里它时成时败（E成/F败/V3败/T0成/T1败/T2成），
本脚本用来在受控条件下反复打点。

前置条件（脚本会自己检查，不满足就拒绝执行）：
  - 设备在总线上且配置空间能读
  - LINK_CAP2 (0x880A4) = 0x0006（声明已解锁）
  - 两端 LNKCTL2 的 TLS 都已是 2
只有这三条都满足，才写 SPEED —— 避免把"别的问题"算到 SPEED 头上。

用法：sudo python3 speed-once.py [--force]
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
    return int(s, 16) if s else None

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

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    force = "--force" in sys.argv

    vid = pci(DEV, "0.l")
    print(f"配置空间 0x00 = {'%08x' % vid if vid is not None else 'N/A'}")
    if vid is None or vid == 0xFFFFFFFF:
        print("设备不应答配置读，先重启"); return 1

    fd, m = bar()
    cap2 = rd(m, R_CAP2)
    ep_tls = pci(DEV, "CAP_EXP+0x30.w")
    rp_tls = pci(ROOT, "CAP_EXP+0x30.w")
    lk = rd(m, R_LNKCFG0)
    ls = pci(DEV, "CAP_EXP+0x12.w")
    print(f"LINK_CAP2 = {cap2:#06x}   0x8C040 = {lk:#010x}  SPEED[19:18]={(lk>>18)&3}")
    print(f"端点 TLS2 = {ep_tls:#06x}  根端口 TLS2 = {rp_tls:#06x}")
    print(f"端点 LNKSTA = {ls:#06x}  sp = {ls & 0xF}")
    print(f"驱动绑定 = {os.path.exists(f'/sys/bus/pci/devices/{DEV}/driver')}")

    ok = True
    if (cap2 & 0x6) != 0x6 and not force:
        print("\n[拒绝] 声明未解锁（CAP2 应为 0x06）。加 --force 强制继续。"); ok = False
    if ep_tls is not None and (ep_tls & 0xF) != 2 and not force:
        print(f"\n[拒绝] 端点 TLS≠2（{ep_tls:#06x}）。加 --force 强制继续。"); ok = False
    if rp_tls is not None and (rp_tls & 0xF) != 2 and not force:
        print(f"\n[拒绝] 根端口 TLS≠2（{rp_tls:#06x}）。加 --force 强制继续。"); ok = False
    if not ok:
        m.close(); os.close(fd); return 1

    if ((lk >> 18) & 3) == 1:
        print("\nSPEED 已经是 1，无需写。")
    else:
        new = (lk & ~0x000C0000) | (1 << 18)     # 只改 SPEED，绝不发 COMMIT
        print(f"\n写入 0x8C040 = {new:#010x}（SPEED=1，无 COMMIT）")
        wr(m, R_LNKCFG0, new)
        if not alive(m):
            print("!! 写完 BAR0 连续 20 次 0xffffffff —— 端点已死")
            m.close(); os.close(fd); return 2
        print(f"  回读 0x8C040 = {rd(m, R_LNKCFG0):#010x}  CAP2 = {rd(m, R_CAP2):#06x}")

    time.sleep(1.0)
    ls = pci(DEV, "CAP_EXP+0x12.w"); rp = pci(ROOT, "CAP_EXP+0x12.w")
    print(f"\n结果: 端点 LNKSTA={ls:#06x} sp={ls & 0xF} | 根端口={rp:#06x}")
    m.close(); os.close(fd)

    if (ls & 0xF) >= 2:
        print("★ Gen2 达成")
        print("\n带宽实测：")
        print(sh("timeout 120 /data/labs/nvidia/pcie-gen2/bwtest 256 10 2>&1").stdout.strip())
    else:
        print("仍是 Gen1")
    return 0

if __name__ == "__main__":
    sys.exit(main())
