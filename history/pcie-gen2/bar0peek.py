#!/usr/bin/env python3
"""bar0peek.py —— 打印几个 BAR0 关键寄存器的当前值（只读，零风险）

用来判断"PCIe 核是否被重新初始化"：
  0x8841C 默认 0xc0b40500（我们改成 0xc0b4ad00 作为标记）
  0x8C040 默认 0x20085800（SPEED[19:18]=2）
若某操作之后这两者回到默认 ⇒ 核被复位了。
"""
import mmap, os, struct, subprocess, sys

PHYS = 0xF6000000
REGS = [("0x8841C CONFIG  ", 0x8841C), ("0x88610 VSEC    ", 0x88610),
        ("0x8872C LTSSM   ", 0x8872C), ("0x8C040 LINKCFG0", 0x8C040),
        ("0x880A4 LINKCAP2", 0x880A4), ("0x8C080 WIDTH   ", 0x8C080)]

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    tag = sys.argv[1] if len(sys.argv) > 1 else ""
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)
    out = []
    for name, off in REGS:
        v = struct.unpack_from("<I", m, off)[0]
        out.append(f"{name}={v:#010x}")
        if v == 0xFFFFFFFF:
            print(f"[{tag}] MMIO 失效（0xffffffff）"); m.close(); return 2
    print(f"[{tag}] " + "  ".join(out))
    m.close(); os.close(fd)
    return 0

if __name__ == "__main__":
    sys.exit(main())
