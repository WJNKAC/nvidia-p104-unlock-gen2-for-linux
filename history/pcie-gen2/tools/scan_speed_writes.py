#!/usr/bin/env python3
"""scan_speed_writes.py —— 统计 nvidia.ko 里所有设置 0x8C040.SPEED 字段的指令

目的（reports/185）：
  我们打了 1 字节补丁把「sp==1 → SPEED=2」改成「sp==1 → SPEED=1」，
  但 A/B 实测发现补丁**没有效果** —— 每次 nvidia-smi 仍然触发一次 SPEED 被改回 2。
  ⇒ 说明还有别处也在写 SPEED=2。

本脚本按"立即数 OR/AND/MOV"模式统计所有可能设置 SPEED 的指令，
并按 .text 内偏移排序，方便判断哪些落在同一个函数区。
"""
import os
import sys

KO = "/lib/modules/%s/updates/dkms/nvidia.ko" % os.uname().release
TEXT_OFF = 0x40
TEXT_SZ = 15333236

PATTERNS = [
    ("or  eax, 0x80000  → SPEED=2(Gen1)", bytes.fromhex("0d00000800")),
    ("or  r8d, 0x80000  → SPEED=2",        bytes.fromhex("4181c800000800")),
    ("or  eax, 0x40000  → SPEED=1(Gen2)", bytes.fromhex("0d00000400")),
    ("or  r8d, 0x40000  → SPEED=1",        bytes.fromhex("4181c800000400")),
    ("or  eax, 0xc0000  → SPEED=3(Gen3)", bytes.fromhex("0d00000c00")),
    ("or  r8d, 0xc0000  → SPEED=3",        bytes.fromhex("4181c800000c00")),
    ("and eax, ~0xC0000 (clear SPEED)",   bytes.fromhex("25fffff3ff")),
    ("and r8d, ~0xC0000",                  bytes.fromhex("4181e0fffff3ff")),
    ("mov ecx, 0x8c040 (offset as arg)",   bytes.fromhex("b940c00800")),
]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else KO
    if not os.path.exists(path):
        print("找不到", path)
        return 1
    with open(path, "rb") as f:
        d = f.read()
    print(f"文件 {path}")
    print(f"大小 {len(d):,}  .text = [file {TEXT_OFF:#x}, +{TEXT_SZ:,}]\n")

    for name, pat in PATTERNS:
        hits, i = [], 0
        while True:
            i = d.find(pat, i)
            if i < 0:
                break
            hits.append(i)
            i += 1
        intxt = [h for h in hits if TEXT_OFF <= h < TEXT_OFF + TEXT_SZ]
        print(f"{name}:  总计 {len(hits)}，.text 内 {len(intxt)}")
        for h in intxt[:12]:
            print(f"    .text+{h - TEXT_OFF:#0x}   (file {h:#x})")
        if len(intxt) > 12:
            print(f"    …… 还有 {len(intxt) - 12} 处")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
