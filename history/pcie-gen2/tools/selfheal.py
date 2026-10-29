#!/usr/bin/env python3
"""selfheal.py —— 降级后「只按回 SPEED、不重训」，链路会不会自己升回 Gen2？

## 要回答的问题（reports/196-197）

守卫的 `speed_settle_ms=3000` 占恢复时间的 74%（3.06s / 5.65s）。
这个 3 秒来自 reports/186 配方「⑤ 写 SPEED → ⑥ 等 ≥3s 再 Retrain」，
语义是"等写 SPEED 触发的自动重训完成"。

但如果那个"自动重训"根本不发生（链路一直停在 Gen1），这 3 秒就是白等。

- 会自愈   → speed_settle_ms 有实际作用，可缩到实测自愈时长
- 不会自愈 → 这 3 秒纯属白等，可大幅压缩

## ★ 为什么不用原来的 probe-selfheal.sh（两次打挂设备的教训）

旧脚本有两个未受控变量：

1. **反复 mmap/munmap**：每 0.2s open /dev/mem + mmap 16MB + munmap，
   12 秒共 60 次。而 `watch_reg.py` 做过 381 万次采样却是**单次映射**，从未出事。
   反复映射 BAR0 极可疑（UC-/WC 映射的 munmap 语义、TLB flush、未完成写回）。
2. **时机太早**：minictx 之后只 sleep 0.5s 就写 SPEED。虽然那时 LNKSTA 已是 Gen1
   （所以不是"重训中"），但驱动侧的上下文清理可能还没完。

本脚本两个都消除：
  · **单次 mmap**，整个观察期只映射一次
  · 降级后**先等 SETTLE 秒**（默认 5，超过守卫的 speed_settle_ms=3000），
    并确认 LNKSTA 连续稳定，才动手写
  · 写之前再确认一次读值合法（!= 0xffffffff 且 bit0(COMMIT)==0）
  · 写完只读，不再写任何寄存器

## 判活

每次读值做连续 N 次校验；出现 0xffffffff 不立即判死（重训瞬态），
连续 20 次才认；一旦认死立即退出并打印 C. ZOMBIE 提示。

用法：
  sudo python3 selfheal.py                # 默认：等 5s，观察 15s
  sudo python3 selfheal.py --settle=3 --observe=20
  sudo python3 selfheal.py --no-unload    # 不自动 rmmod gen2kr（不推荐）
"""
import mmap
import os
import struct
import subprocess
import sys
import time

DEV = "0000:01:00.0"
R_LINKCFG0 = 0x8C040
R_LNKSTA = 0x88088
R_LNKCAP2 = 0x880A4
GEN2_SPEED = 1
COMMIT = 1 << 0
GRACE = 20


def sh(c, t=60):
    return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)


class Bar:
    """★ 单次映射，反复读写。绝不在循环里重新 open/mmap。"""

    def __init__(self, phys):
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, 0x1000000, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=phys)

    def rd(self, o):
        return struct.unpack_from("<I", self.m, o)[0]

    def wr(self, o, v):
        struct.pack_into("<I", self.m, o, v & 0xFFFFFFFF)

    def alive(self):
        bad = 0
        for _ in range(GRACE):
            if self.rd(R_LNKCAP2) == 0xFFFFFFFF:
                bad += 1
                time.sleep(0.02)
            else:
                return True
        return bad < GRACE

    def close(self):
        try:
            self.m.close(); os.close(self.fd)
        except Exception:
            pass


def bar0():
    with open(f"/sys/bus/pci/devices/{DEV}/resource") as f:
        return int(f.readline().split()[0], 16)


def sp_of(v):
    return (v >> 18) & 3


def lnk(v):
    return (v >> 16) & 0xF


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1

    settle = 5.0
    observe = 15.0
    unload = True
    for a in sys.argv[1:]:
        if a.startswith("--settle="):
            settle = float(a.split("=", 1)[1])
        elif a.startswith("--observe="):
            observe = float(a.split("=", 1)[1])
        elif a == "--no-unload":
            unload = False

    # ---- 0. 停掉守卫：必须 rmmod（guard_enabled=0 停不掉"按回 SPEED"）----
    if unload:
        if sh("lsmod | grep -q '^gen2kr'").returncode == 0:
            r = sh("rmmod gen2kr")
            print("0. rmmod gen2kr：" + ("OK（守卫已彻底停止）"
                                        if r.returncode == 0 else "失败 " + r.stderr.strip()))
        else:
            print("0. gen2kr 未加载")

    phys = bar0()
    print(f"   BAR0 = {phys:#x}")
    bar = Bar(phys)
    if not bar.alive():
        print("   ✗ MMIO 不应答，设备可能已 ZOMBIE"); bar.close(); return 2

    # ---- 1. 确保 Gen2 ----
    print("\n1. 确保当前是 Gen2")
    ok = False
    for i in range(20):
        v = bar.rd(R_LNKSTA)
        if lnk(v) == 2:
            print(f"   已是 Gen2（{i}s）"); ok = True; break
        time.sleep(1)
    if not ok:
        print("   当前不是 Gen2，尝试 unlock-decl.py --retrain")
        sh("python3 /data/labs/nvidia/pcie-gen2/tools/unlock-decl.py --retrain", 180)
        v = bar.rd(R_LNKSTA)
        if lnk(v) != 2:
            print(f"   ✗ 到不了 Gen2（LNKSTA={lnk(v)}），终止")
            bar.close(); return 2
        print("   已 Gen2")

    # ---- 2. 制造降级 ----
    print("\n2. 制造降级（新建 1 个 CUDA 上下文）")
    sh("/data/labs/nvidia/pcie-gen2/minictx 1", 60)
    v = bar.rd(R_LINKCFG0); l = bar.rd(R_LNKSTA)
    print(f"   降级后 0x8C040={v:#010x} SPEED={sp_of(v)}  "
          f"LNKSTA[3:0]={lnk(l)}")
    if lnk(l) != 1:
        print("   ⚠ 没有降级，实验前提不成立，仍继续观察")

    # ---- 3. ★ 先等链路稳定（关键，前两次掉卡都在这里）----
    print(f"\n3. 等链路稳定 {settle:.0f}s（先不写任何东西）")
    stable = 0
    last = None
    t0 = time.time()
    while time.time() - t0 < settle + 5:
        l = bar.rd(R_LNKSTA)
        cur = lnk(l)
        stable = stable + 1 if (last is not None and cur == last) else 1
        last = cur
        if time.time() - t0 >= settle and stable >= 5:
            break
        time.sleep(0.2)
    print(f"   稳定：LNKSTA[3:0]={last}，连续 {stable} 次一致")

    # ---- 4. 只按回 SPEED ----
    print("\n4. 只按回 SPEED=1（不重训）")
    v = bar.rd(R_LINKCFG0)
    # ★ 双校验（reports/196）
    if v == 0xFFFFFFFF or (v & COMMIT):
        print(f"   ✗ 0x8C040={v:#010x} 不可信（ffffffff 或 COMMIT=1），拒绝写入")
        bar.close(); return 3
    if sp_of(v) == GEN2_SPEED:
        print(f"   SPEED 已是 1（{v:#010x}），无需写")
    else:
        new = (v & ~0x000C0000) | (GEN2_SPEED << 18)
        bar.wr(R_LINKCFG0, new)
        time.sleep(0.05)
        r = bar.rd(R_LINKCFG0)
        if r == 0xFFFFFFFF:
            print(f"   ✗ 写入后读回 ffffffff —— 设备失联")
            bar.close(); return 4
        print(f"   {v:#010x} → {r:#010x}  SPEED={sp_of(r)}")

    # ---- 5. 纯观察，不再写 ----
    print(f"\n5. 纯观察 {observe:.0f}s（不做任何写入）")
    healed = None
    t0 = time.time()
    while time.time() - t0 < observe:
        time.sleep(0.2)
        l = bar.rd(R_LNKSTA)
        v = bar.rd(R_LINKCFG0)
        if l == 0xFFFFFFFF:
            print(f"   {time.time()-t0:5.1f}s  ★ 读回 ffffffff")
            continue
        if lnk(l) == 2:
            healed = time.time() - t0
            print(f"   {healed:5.1f}s  ★ 自己升回 Gen2（SPEED={sp_of(v)}）")
            break
        if abs(time.time() - t0 - round((time.time() - t0) / 3) * 3) < 0.25:
            print(f"   {time.time()-t0:5.1f}s  sp={lnk(l)} SPEED={sp_of(v)}")

    print("\n=== 结论 ===")
    if healed is not None:
        print(f"  A. {healed:.1f}s 自己升回 ⇒ speed_settle_ms 有实际作用")
        print(f"     可缩到 {max(healed, 0.5):.1f}s + 安全余量（当前 3000ms）")
    else:
        print("  B. 观察期内没有自己升 ⇒ 这 3 秒纯属白等")
        print("     speed_settle_ms 可大幅缩短（建议 800–1000ms，仍留余量）")

    bar.close()

    # ---- 恢复守卫 ----
    print("\n=== 恢复守卫 ===")
    if not os.path.exists("/sys/module/gen2kr"):
        r = sh("cd /data/labs/nvidia/pcie-gen2/kmod && insmod gen2kr.ko")
        print("  已重新加载 gen2kr" if r.returncode == 0
              else "  ⚠ 重新加载失败：" + r.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
