#!/usr/bin/env python3
"""unlock-decl.py —— 只做「解锁声明 + 触发训练」，验证补丁后链路能否自愈

## 为什么需要它（reports/195）

`_nv017281rm` 每次 cuCtxCreate 都会把 `0x8C040.SPEED` 写成"当前链路速率"：
  · 补丁前：sp==1 → 写 SPEED=2 (0x80000) → 2.5GT/s → **锁死 Gen1**
  · 补丁后：sp==1 → 写 SPEED=1 (0x40000) → **请求 5.0GT/s**

补丁已实测生效（R8 从 0x20085800 → 0x20045800）。
但链路仍是 Gen1，因为**声明 `LINK_CAP2` 还是 0x0002（只支持 Gen1）**，硬件拒绝协商 5GT/s。

所以只要把声明解锁，再触发一次 LTSSM 训练，链路**应该自己升到 5GT/s** ——
因为目标速率 SPEED=1 已经由驱动（补丁后）写好了。

这比 safe-gen2.py 少做两件事，也因此更安全：
  · **不写 0x8C040.SPEED**（已经是 1，不需要我们写；避开了"训练未完成时写 SPEED"这条必死路径）
  · **不做 ⑤→⑥ 的强制重训**（除非必要，且只从根端口发）

## 安全约束（沿用 reports/186 的定案）
  ① 解锁声明：写 XP3G 四寄存器 + `0x8841C` **读值再 OR**（绝不写绝对值）+ `0x88610`
  ② `0x8872C = 6` 触发训练，然后**至少等 5s / 轮询 LCAP2 变 0x06**
  ③ 只从**根端口** Retrain，**绝不碰端点**
  ④ 每次高危写后回读；连续 20 次 0xffffffff 才判死

用法：
  sudo python3 unlock-decl.py --dry-run
  sudo python3 unlock-decl.py
  sudo python3 unlock-decl.py --retrain      # 若训练后仍未升，补一次根端口 Retrain
"""
import mmap
import os
import struct
import subprocess
import sys
import time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
GRACE = 20

R_LNKCAP2 = 0x880A4
R_LNKSTA = 0x88088
R_CONFIG = 0x8841C
R_VSECDEV = 0x8860C
R_VSEC = 0x88610
R_LTSSM = 0x8872C
R_SPEED = 0x8C040
R_CYA = 0x8C2C0

XP3G = ((0x8E110, 0x00000001, "XP3G_OVR0"),
        (0x8E11C, 0x00000004, "XP3G_OVR3"),
        (0x8E120, 0x00000000, "XP3G_VAL0"),
        (0x8E12C, 0x00200000, "XP3G_VAL3"))


def sh(c, t=60):
    return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)


def bar0_addr():
    try:
        with open(f"/sys/bus/pci/devices/{DEV}/resource") as f:
            return int(f.readline().split()[0], 16)
    except Exception:
        return None


def pci(sel, reg):
    r = sh(f"setpci -s {sel} {reg}")
    s = r.stdout.strip()
    try:
        return int(s, 16)
    except Exception:
        return None


class Bar:
    def __init__(self, phys):
        # ★ 必须 O_RDWR：O_RDONLY 打开后再请求 PROT_WRITE 会 PermissionError
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.pg = 0
        self.m = mmap.mmap(self.fd, 0x1000000, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=phys)

    def rd(self, o):
        return struct.unpack_from("<I", self.m, o - self.pg)[0]

    def wr(self, o, v):
        struct.pack_into("<I", self.m, o - self.pg, v & 0xFFFFFFFF)

    def alive(self):
        """★ 连续 20 次 0xffffffff 才判死（单次是重训瞬态）"""
        bad = 0
        for _ in range(GRACE):
            if self.rd(R_LNKCAP2) == 0xFFFFFFFF:
                bad += 1
            else:
                return True
            time.sleep(0.05)
        return False

    def close(self):
        try:
            self.m.close(); os.close(self.fd)
        except Exception:
            pass


def show(tag, bar):
    sp = bar.rd(R_SPEED)
    ls = bar.rd(R_LNKSTA)
    print(f"[{tag}]")
    print(f"  LCAP2(声明) = {bar.rd(R_LNKCAP2):#06x}"
          f"   {'✓ 已解锁' if bar.rd(R_LNKCAP2) == 0x06 else '（未解锁，含 Gen1 only）'}")
    print(f"  0x8C040    = {sp:#010x}   SPEED={(sp >> 18) & 3}"
          f"  (1=5GT/s  2=2.5GT/s)")
    print(f"  0x88088    = {ls:#010x}   LNKSTA[3:0]={(ls >> 16) & 0xF}"
          f"   {'Gen2' if ((ls >> 16) & 0xF) == 2 else 'Gen1' if ((ls >> 16) & 0xF) == 1 else '?'}")


def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    dry = "--dry-run" in sys.argv
    do_retrain = "--retrain" in sys.argv

    phys = bar0_addr()
    if not phys:
        print("取不到 BAR0"); return 1
    print(f"BAR0 = {phys:#x}   模式={'dry-run' if dry else '执行'}\n")

    bar = Bar(phys)
    if not bar.alive():
        print("MMIO 不应答 —— 先跑 pci-state.sh"); bar.close(); return 1

    show("⓪ 起点", bar)
    if dry:
        print("\n--dry-run：只看不改。"); bar.close(); return 0

    # ---- ① 解锁声明 ----
    print("\n=== ① 解锁声明（从不掉卡）===")
    for off, val, nm in XP3G:
        bar.wr(off, val)
        print(f"  {nm:10s}= {val:#010x}  {'✓' if bar.rd(off) == val else '✗'}")

    c = bar.rd(R_CONFIG)
    newc = (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14))
    bar.wr(R_CONFIG, newc)
    print(f"  0x8841C    {c:#010x} → {bar.rd(R_CONFIG):#010x}  (读值再OR, 置15/13/11 清12/14)")

    g = bar.rd(R_VSEC)
    bar.wr(R_VSEC, (g & ~(1 << 12)) | 1)
    print(f"  0x88610    {g:#010x} → {bar.rd(R_VSEC):#010x}")

    v = bar.rd(R_VSECDEV)
    bar.wr(R_VSECDEV, v | 1)
    print(f"  0x8860C    {v:#010x} → {bar.rd(R_VSECDEV):#010x}")

    cya = bar.rd(R_CYA)
    if (cya >> 2) & 1:
        bar.wr(R_CYA, cya & ~(1 << 2)); print(f"  0x8C2C0    DIS_G2 已清")
    else:
        print(f"  0x8C2C0    = {cya:#010x}  DIS_G2 本就 0")

    if not bar.alive():
        print("!! ① 之后 MMIO 无应答，立即停止"); bar.close(); return 2

    # ---- ② 触发训练 ----
    print("\n=== ② 写 0x8872C = 6（触发动 LTSSM 训练；写本身安全）===")
    bar.wr(R_LTSSM, 6)
    print(f"  0x8872C    = {bar.rd(R_LTSSM):#x}")

    # ---- ③ 等训练完成 ----
    print("\n=== ③ 等声明锁存（LCAP2 → 0x06），至少 5s ===")
    t0 = time.time()
    latched = False
    while time.time() - t0 < 25:
        if not bar.alive():
            print("!! ③ 期间 MMIO 无应答"); bar.close(); return 2
        v2 = bar.rd(R_LNKCAP2)
        if v2 == 0x06:
            latched = True
            print(f"  {time.time()-t0:.1f}s 后 LCAP2 = 0x06  ✓ 锁存")
            break
        time.sleep(0.5)
    if not latched:
        print(f"  ✗ 25s 内未锁存，LCAP2 = {bar.rd(R_LNKCAP2):#06x}")

    show("③ 训练后", bar)

    # ---- ④ 看 SPEED 是否已由驱动设好 ----
    print("\n=== ④ 检查 0x8C040.SPEED（补丁后应由驱动写成 1）===")
    sp = bar.rd(R_SPEED)
    speed = (sp >> 18) & 3
    if speed == 1:
        print(f"  SPEED = 1 (5.0GT/s)  ✓ 补丁已生效，目标速率正确")
    else:
        print(f"  SPEED = {speed}  ⚠ 不是 1 —— 补丁可能未生效，或驱动尚未写过")

    # ---- ⑤ 可选：根端口 Retrain ----
    if do_retrain:
        print("\n=== ⑤ 从【根端口】Retrain（绝不碰端点），最多 6 轮 ===")
        for i in range(1, 7):
            if not bar.alive():
                print(f"!! 第 {i} 轮前 MMIO 无应答"); bar.close(); return 2
            cur = pci(ROOT, "CAP_EXP+0x10.w")
            if cur is None:
                print("  根端口读不到 LNKCTL"); break
            sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={((cur | (1 << 5)) & 0xFFFF):04x}")
            time.sleep(1.5)
            ls = bar.rd(R_LNKSTA)
            sp2 = (ls >> 16) & 0xF
            print(f"  第 {i} 轮后 LNKSTA[3:0] = {sp2}  {'★Gen2' if sp2 == 2 else ''}")
            if sp2 == 2:
                break

    show("最终", bar)

    print("\n=== 物理链路（lspci 官方判据）===")
    r = sh(f"lspci -vv -s {DEV} 2>/dev/null | grep -o 'LnkSta:.*' | head -1")
    print("  " + (r.stdout.strip() or "(读不到)"))

    bar.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
