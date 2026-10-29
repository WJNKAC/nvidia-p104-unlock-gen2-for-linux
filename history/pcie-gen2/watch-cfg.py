#!/usr/bin/env python3
"""watch-cfg.py —— 零风险观察：不写任何寄存器，只看驱动会不会自己置 bit15/bit11

背景（reports/177 §7）：
  E 组（唯一成功）起点 0x8841C = 0xc0b48d00，已经含 bit15 + bit11；
  F/V3（掉卡）起点 = 0xc0b40500，都没有。
  源码注释说 bit15 决定声明能否锁存、且"时好时坏"= 驱动置位时序。

本脚本只读，判定：
  驱动若在启动后 N 秒内把 bit15/bit11 置上，则我们的正确做法是"等它置完再操作"。
"""
import mmap, os, struct, subprocess, sys, time

PHYS = 0xF6000000
DEV = "0000:01:00.0"

def bits(name, v, mask):
    return " ".join(f"{name}{i}={1 if v & (1 << i) else 0}"
                    for i in range(32) if mask & (1 << i))

def rd(m, o): return struct.unpack_from("<I", m, o)[0]

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

    print(f"只读观察 {dur}s，每 2s 一次（不写任何寄存器）")
    print("关注 0x8841C 的 bit15 / bit11，以及 CAP2(0x880A4)")
    t0 = time.time(); prev = None
    while time.time() - t0 < dur:
        c  = rd(m, 0x8841C)
        c2 = rd(m, 0x880A4)
        lk = rd(m, 0x8C040)
        ls = rd(m, 0x88088) >> 16
        mark = ""
        if prev is not None and c != prev:
            mark = "  ← 变化!"
        print(f"[{time.time()-t0:5.1f}s] cfg={c:#010x} b15={1 if c&(1<<15) else 0} "
              f"b11={1 if c&(1<<11) else 0} b13={1 if c&(1<<13) else 0} "
              f"| CAP2={c2:#06x} lnkcfg0={lk:#010x} LNKSTA={ls:#06x}{mark}")
        prev = c
        time.sleep(2)

    print("\n最终:")
    c = rd(m, 0x8841C)
    print(f"  cfg      = {c:#010x}   {bits('bit', c, 0xFFFF0000)}")
    print(f"  CAP2     = {rd(m,0x880A4):#06x}")
    print(f"  LNKSTA   = {rd(m,0x88088)>>16:#06x}")
    m.close(); os.close(fd)
    return 0

if __name__ == "__main__":
    sys.exit(main())
