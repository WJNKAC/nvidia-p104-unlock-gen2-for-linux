#!/usr/bin/env python3
"""peek-regs.py —— 只读打印关键 BAR0 寄存器（不写）

用法：sudo python3 peek-regs.py
"""
import mmap
import os
import struct
import sys

REGS = [
    (0x88084, "LNKCAP 镜像 (MaxSpeed=bits[3:0])"),
    (0x880A4, "LNKCAP2 镜像 (声明向量, 0x06=解锁)"),
    (0x88088, "LNKSTA 镜像 (CurSpeed=bits[19:16])"),
    (0x880A8, "LNKCTL2 镜像 (TLS=bits[3:0])"),
    (0x8841C, "PRIV_MISC_1 (声明位)"),
    (0x8872C, "XVE_OVR (写 6 = 触发训练)"),
    (0x8C040, "LINK_CONFIG_0 (SPEED=bits[19:18])"),
    (0x8C2C0, "CYA_0 (bit2 = DIS_G2)"),
]


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    p = int(open("/sys/bus/pci/devices/0000:01:00.0/resource")
            .readline().split()[0], 16)
    f = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    print(f"BAR0 = {p:#x}")
    for off, desc in REGS:
        v = struct.unpack_from("<I", m, off)[0]
        extra = ""
        if off == 0x88084:
            extra = f"  MaxSpeed={v & 0xF}"
        elif off == 0x88088:
            extra = f"  CurSpeed={(v >> 16) & 0xF}"
        elif off == 0x8C040:
            extra = f"  SPEED={(v >> 18) & 3}"
        elif off == 0x880A8:
            extra = f"  TLS={v & 0xF}"
        print(f"  0x{off:05x} = 0x{v:08x}   {desc}{extra}")
    m.close(); os.close(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
