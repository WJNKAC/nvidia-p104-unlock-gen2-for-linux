#!/usr/bin/env python3
"""force-gen2.py —— 反复尝试把链路推到 Gen2（只读判定，用寄存器不用 lspci 文本）

## 为什么需要（reports/198）
在"驱动被反复 reload"之后，push 会失败：端点 `LnkCtl2.TLS` 被钳回 1，
`LnkCap2` 声明也停在 0x02。此时生成器（gen2kr）与驱动互相抢，需要重试多次。

## ★ 判定必须用寄存器
`grep 'Speed 5GT/s'` 会误匹配 `Speed 2.5GT/s`（子串），这个坑已踩过两次。
唯一可信：`LNKSTA[3:0] == 2`。

用法（root）：
  python3 force-gen2.py [尝试次数]
"""
import mmap
import os
import struct
import subprocess
import sys
import time

DEV = "0000:01:00.0"


def regs():
    p = int(open(f"/sys/bus/pci/devices/{DEV}/resource").readline().split()[0], 16)
    f = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    a = struct.unpack_from("<I", m, 0x880A4)[0]
    l = struct.unpack_from("<I", m, 0x88088)[0]
    t = struct.unpack_from("<I", m, 0x880A8)[0]
    s = struct.unpack_from("<I", m, 0x8C040)[0]
    m.close(); os.close(f)
    return a, (l >> 16) & 0xF, t & 0xF, (s >> 18) & 3


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    tries = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    a, sp, tls, sd = regs()
    print(f"起点: CAP2=0x{a:04x} LNKSTA={sp} TLS={tls} SPEED={sd}")
    if sp == 2:
        print("已经是 Gen2"); return 0

    for i in range(1, tries + 1):
        subprocess.run(f"echo {DEV} > /sys/bus/pci/drivers/nvidia/unbind",
                       shell=True, capture_output=True)
        time.sleep(0.8)
        subprocess.run(f"echo {DEV} > /sys/bus/pci/drivers/nvidia/bind",
                       shell=True, capture_output=True)
        # 最多等 10s
        for _ in range(20):
            time.sleep(0.5)
            a, sp, tls, sd = regs()
            if sp == 2:
                break
        print(f"  第{i}次: CAP2=0x{a:04x} LNKSTA={sp} TLS={tls} SPEED={sd}")
        if sp == 2:
            print("  ✓ Gen2 达成")
            return 0

    print("  ✗ 多次尝试仍未到 Gen2")
    print("    提示：若 CAP2 一直 0x0002 且 TLS 一直是 1，说明声明/TLS 被驱动钳住，")
    print("          需要整机重启回到干净状态（reports/198 结论）。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
