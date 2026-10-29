#!/usr/bin/env python3
"""watch_reg.py —— 高频采样 BAR0 的某个寄存器，找出它变化的确切时刻

用途：验证"驱动到底写不写 0x8C040"。
  如果整个 CUDA 启动过程采样 100 万次都没有变化 ⇒ 驱动确实不写它，
  那链路掉 Gen1 一定是**别的原因**（不是软件写 SPEED）。

用法：
  sudo python3 watch_reg.py <秒数> [--during "命令"]

  例：sudo python3 watch_reg.py 8 --during "/data/labs/nvidia/pcie-gen2/bwtest2 64 3"
"""
import mmap
import os
import struct
import subprocess
import sys
import time

R_SPEED  = 0x8C040
R_LNKSTA = 0x88088
R_CAP2   = 0x880A4


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    cmd = None
    if "--during" in sys.argv:
        cmd = sys.argv[sys.argv.index("--during") + 1]

    # BAR0 从 sysfs 取（比 setpci 可靠）
    with open("/sys/bus/pci/devices/0000:01:00.0/resource") as f:
        bar0 = int(f.readline().split()[0], 16)
    print(f"BAR0={bar0:#x}  目标={bar0 + R_SPEED:#x}  时长={dur}s")
    if cmd:
        print(f"期间将执行: {cmd}")

    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=bar0)

    def rd(o):
        return struct.unpack_from("<I", m, o)[0]

    sp0 = rd(R_SPEED)
    st0 = rd(R_LNKSTA) >> 16
    print(f"起点: 0x8C040={sp0:#010x}(SPEED={(sp0 >> 18) & 3})  "
          f"LNKSTA={st0:#06x}(sp={st0 & 0xF})")

    proc = None
    if cmd:
        proc = subprocess.Popen(cmd, shell=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    t0 = time.time()
    n = 0
    sp_changes = []
    st_changes = []
    prev_sp, prev_st = sp0, st0
    first_dead = None

    while time.time() - t0 < dur:
        v = rd(R_CAP2)
        if v == 0xFFFFFFFF:
            if first_dead is None:
                first_dead = time.time() - t0
            time.sleep(0.01)
            continue
        sp = rd(R_SPEED)
        st = rd(R_LNKSTA) >> 16
        n += 1
        if sp != prev_sp:
            sp_changes.append((time.time() - t0, prev_sp, sp))
            prev_sp = sp
        if st != prev_st:
            st_changes.append((time.time() - t0, prev_st, st))
            prev_st = st
        if proc and proc.poll() is not None:
            pass
        # 不 sleep，全力采样

    if proc:
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()

    print(f"\n采样 {n} 次（{n / max(dur, 0.001):.0f} 次/秒）")
    print(f"0x8C040 变化 {len(sp_changes)} 次:")
    for t, a, b in sp_changes[:20]:
        print(f"  [{t:6.3f}s] {a:#010x}(SPEED={(a >> 18) & 3}) → "
              f"{b:#010x}(SPEED={(b >> 18) & 3})")
    print(f"LNKSTA 变化 {len(st_changes)} 次:")
    for t, a, b in st_changes[:20]:
        print(f"  [{t:6.3f}s] {a:#06x}(sp={a & 0xF}) → {b:#06x}(sp={b & 0xF})")
    if first_dead is not None:
        print(f"\n!! BAR0 首次读到 0xffffffff 于 {first_dead:.3f}s（MMIO 短暂不可用）")

    spN = rd(R_SPEED)
    stN = rd(R_LNKSTA) >> 16
    print(f"\n终点: 0x8C040={spN:#010x}(SPEED={(spN >> 18) & 3})  "
          f"LNKSTA={stN:#06x}(sp={stN & 0xF})")
    m.close()
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
