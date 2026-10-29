#!/usr/bin/env python3
"""safe-gen2.py —— 按 reports/186 定案的安全时序推 Gen2

## 为什么要有这个脚本

历史 7 次实验里 2 掉 5 存，根因是**时序**而非概率：
`0x8872C = 6` 触发一次 LTSSM 链路训练，**训练未完成时写 `0x8C040.SPEED` 必死**。

本脚本把所有已知的时序约束固化下来：

  ① 解锁声明（XP3G_OVR/VAL + 0x8841C + 0x88610）      —— 从不掉卡
  ② 写 0x8872C = 6                                     —— 触发训练，写本身安全
  ③ ★ 轮询等待训练完成（LCAP2 变成 0x06），至少等 5s     —— 关键等待
  ④ 两端 TLS = 5.0 GT/s
  ⑤ 写 0x8C040.SPEED = 1（Pascal 编码）                  —— 训练完成后才安全
  ⑥ 等 ≥3s，再从【根端口】发 Retrain                     —— 不碰端点 Retrain
  ⑦ 轮询 LNKSTA

## 安全闸门（每步都查）
  · 每次高危写后回读 LINK_CAP2 镜像；连续 20 次 0xffffffff 才判死
  · 单次 0xffffffff 视为重训瞬态，重试
  · 状态落盘到 state-*.json，掉卡后可复盘

用法：
  sudo python3 safe-gen2.py --dry-run      # 只读，打印所有寄存器
  sudo python3 safe-gen2.py                # 执行
  sudo python3 safe-gen2.py --settle=8     # 自定义 ②→⑤ 的等待秒数
"""
import json
import mmap
import os
import struct
import subprocess
import sys
import time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
SYS = f"/sys/bus/pci/devices/{DEV}"
GRACE = 20

# BAR0 寄存器
R_LNKCAP   = 0x88084
R_LNKCAP2  = 0x880A4   # 声明镜像（判断解锁/存活）
R_LNKCTL2  = 0x880A8   # LNKCTL2 镜像
R_LNKSTA   = 0x88088
R_CONFIG   = 0x8841C   # PRIV_MISC_1
R_VSECDEV  = 0x8860C
R_VSEC     = 0x88610
R_LTSSM    = 0x8872C   # XVE_OVR，写 6 = 触发训练
R_SPEED    = 0x8C040   # LINK_CONFIG_0，SPEED[19:18]
R_PLRATE   = 0x8C1C0
R_CYA      = 0x8C2C0   # bit2 = DIS_G2
R_WIDTH    = 0x8C080

GEN2_SPEED = 1          # ★ Pascal: 1 = 5.0 GT/s（Ampere 才是 2）
SETTLE = 5.0            # ★ ②→⑤ 的等待，默认 5s（铁律）
POST_SPEED_WAIT = 3.0   # ★ ⑤→⑥ 的等待，默认 3s


def sh(c, t=180):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(c, 124, "", "TIMEOUT")


def pci(sel, reg, tries=1):
    for _ in range(tries):
        s = sh(f"setpci -s {sel} {reg}").stdout.strip()
        try:
            if s:
                return int(s, 16)
        except ValueError:
            pass
        if tries > 1:
            time.sleep(0.02)
    return None


def bar0_addr():
    try:
        with open(f"{SYS}/resource") as f:
            return int(f.readline().split()[0], 16)
    except Exception:
        return None


class Bar:
    def __init__(self, phys):
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, 0x1000000, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=phys)

    def rd(self, o):
        return struct.unpack_from("<I", self.m, o)[0]

    def wr(self, o, v):
        struct.pack_into("<I", self.m, o, v)

    def alive(self):
        """连续 GRACE 次 0xffffffff 才判死（单次是重训瞬态）"""
        for _ in range(GRACE):
            if self.rd(R_LNKCAP2) != 0xFFFFFFFF:
                return True
            time.sleep(0.02)
        return False

    def close(self):
        try:
            self.m.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


STATEF = ""


def dump(tag, bar, extra=None):
    d = {"tag": tag, "t": time.strftime("%F %T")}
    if bar is not None:
        for k, off in (("lnkcap", R_LNKCAP), ("lnkcap2", R_LNKCAP2),
                       ("lnkctl2", R_LNKCTL2), ("config", R_CONFIG),
                       ("vsec", R_VSEC), ("ltssm", R_LTSSM),
                       ("speed", R_SPEED), ("cya", R_CYA), ("width", R_WIDTH)):
            d[k] = f"{bar.rd(off):#010x}"
    for k, (sel, reg) in (("ep_tls2", (DEV, "CAP_EXP+0x30.w")),
                          ("ep_lnksta", (DEV, "CAP_EXP+0x12.w")),
                          ("rp_tls2", (ROOT, "CAP_EXP+0x30.w")),
                          ("rp_lnksta", (ROOT, "CAP_EXP+0x12.w"))):
        v = pci(sel, reg, tries=3)
        d[k] = None if v is None else f"{v:#06x}"
    if extra:
        d.update(extra)

    print(f"\n[{tag}]")
    if bar is not None:
        sp = bar.rd(R_SPEED)
        cya = bar.rd(R_CYA)
        print(f"  LCAP2={bar.rd(R_LNKCAP2):#06x}  LCAP={bar.rd(R_LNKCAP):#08x}  "
              f"WIDTH={bar.rd(R_WIDTH):#06x}")
        print(f"  0x8841C={bar.rd(R_CONFIG):#010x}  0x88610={bar.rd(R_VSEC):#010x}  "
              f"0x8872C={bar.rd(R_LTSSM):#x}")
        print(f"  0x8C040={sp:#010x}(SPEED={(sp >> 18) & 3})  "
              f"0x8C2C0={cya:#010x}(DIS_G2={(cya >> 2) & 1})")
    print(f"  端点 TLS2={d['ep_tls2']} LNKSTA={d['ep_lnksta']} | "
          f"根端口 TLS2={d['rp_tls2']} LNKSTA={d['rp_lnksta']}")
    pwr = sh(f"cat {SYS}/power_state 2>/dev/null").stdout.strip()
    d["power"] = pwr
    if pwr:
        print(f"  power_state={pwr}")

    if STATEF:
        try:
            with open(STATEF, "a", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return d


def panic(bar, where):
    v = bar.rd(R_LNKCAP2)
    print(f"\n!!! 安全网触发（{where}）：LINK_CAP2 = {v:#010x}")
    print("!!! 端点 MMIO 已死。此时 lspci 可能仍能看到设备，那是假象。")
    print("!!! 恢复：重启（或先试 pci-state.sh 判定三态）")
    try:
        bar.close()
    except Exception:
        pass
    sys.exit(2)


def wait_latch(bar, min_s=5.0, max_s=25.0):
    """★ 核心等待：等 0x8872C=6 触发的训练完成（LCAP2 变 0x06）"""
    print(f"\n=== ② → ③ 等待训练完成（最少 {min_s:.0f}s，最多 {max_s:.0f}s）===")
    t0 = time.time()
    cap2 = None
    while time.time() - t0 < max_s:
        time.sleep(1.0)
        if not bar.alive():
            panic(bar, "等待训练期间")
        cap2 = bar.rd(R_LNKCAP2)
        el = time.time() - t0
        ok = (cap2 & 0x6) == 0x6
        print(f"  [{el:4.1f}s] LCAP2={cap2:#06x}"
              + ("  ✓ 已锁存 Gen2" if ok else "")
              + ("" if el >= min_s else "  (未到最短等待)"))
        if ok and el >= min_s:
            return True
    print(f"  超时，LCAP2={('?' if cap2 is None else format(cap2, '#06x'))}")
    return False


def main():
    global STATEF, SETTLE
    if os.geteuid() != 0:
        print("需要 root")
        return 1
    dry = "--dry-run" in sys.argv
    for a in sys.argv[1:]:
        if a.startswith("--settle="):
            SETTLE = float(a.split("=", 1)[1])

    bar0 = bar0_addr()
    if not bar0:
        print("取不到 BAR0 —— 设备可能不在总线"); return 1

    STATEF = (f"/data/labs/nvidia/pcie-gen2/state-"
              f"{time.strftime('%Y%m%d-%H%M%S')}.json")

    print(f"BAR0 = {bar0:#x}   模式={'dry-run' if dry else '执行'}   "
          f"等训练={SETTLE:.0f}s")

    bar = Bar(bar0)
    if not bar.alive():
        print("MMIO 不应答 —— 先跑 pci-state.sh 判定三态"); bar.close(); return 1

    dump("⓪ 起点", bar)
    if dry:
        print("\n--dry-run：只看不改。")
        bar.close(); return 0

    # ---------- ① 解锁声明 ----------
    print("\n=== ① 解锁声明（从不掉卡）===")
    for off, val, nm in ((0x8E110, 0x00000001, "XP3G_OVR0"),
                         (0x8E11C, 0x00000004, "XP3G_OVR3"),
                         (0x8E120, 0x00000000, "XP3G_VAL0"),
                         (0x8E12C, 0x00200000, "XP3G_VAL3")):
        bar.wr(off, val)
        print(f"  {nm:10s}= {val:#010x}  {'✓' if bar.rd(off) == val else '✗'}")

    # ★ 必须读当前值再 OR，绝不写绝对值（写绝对值会打死端点）
    c = bar.rd(R_CONFIG)
    bar.wr(R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15))
            & ~((1 << 12) | (1 << 14)))
    print(f"  0x8841C   = {c:#010x} → {bar.rd(R_CONFIG):#010x}  (置15/13/11,清12/14)")
    g = bar.rd(R_VSEC)
    bar.wr(R_VSEC, (g & ~(1 << 12)) | 1)
    print(f"  0x88610   = {g:#010x} → {bar.rd(R_VSEC):#010x}  (清12,置0)")
    v = bar.rd(R_VSECDEV)
    bar.wr(R_VSECDEV, v | 1)
    print(f"  0x8860C   = {v:#010x} → {bar.rd(R_VSECDEV):#010x}  (置 bit0)")

    # 清 DIS_G2（P104 本来就是 0，但保留以兼容其他卡）
    cya = bar.rd(R_CYA)
    if (cya >> 2) & 1:
        bar.wr(R_CYA, cya & ~(1 << 2))
        print(f"  0x8C2C0   DIS_G2 已清: {cya:#010x} → {bar.rd(R_CYA):#010x}")
    else:
        print(f"  0x8C2C0   = {cya:#010x}  DIS_G2 本来就 0")

    if not bar.alive():
        panic(bar, "① 解锁声明后")

    # ---------- ② 触发训练 ----------
    print("\n=== ② 写 0x8872C = 6（触发 LTSSM 训练；写本身安全）===")
    bar.wr(R_LTSSM, 6)
    print(f"  0x8872C   = {bar.rd(R_LTSSM):#x}")

    # ---------- ③ ★ 等待训练完成 ----------
    latched = wait_latch(bar, min_s=SETTLE)
    dump("③ 训练完成后", bar)

    # ---------- ④ 两端 TLS ----------
    print("\n=== ④ 两端 LNKCTL2 TLS = 5.0 GT/s ===")
    for sel, nm in ((DEV, "端点  "), (ROOT, "根端口")):
        cur = pci(sel, "CAP_EXP+0x30.w")
        if cur is None:
            print(f"  {nm} 读不到"); continue
        if (cur & 0xF) == 2:
            print(f"  {nm} LNKCTL2 = {cur:#06x}  TLS 已是 2")
        else:
            sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")
            print(f"  {nm} LNKCTL2 {cur:#06x} → {pci(sel,'CAP_EXP+0x30.w'):#06x}")

    # ---------- ⑤ 写 SPEED ----------
    print("\n=== ⑤ 写 0x8C040.SPEED=1（Pascal；训练已完成才安全）===")
    cur = bar.rd(R_SPEED)
    # ★★★ 读值校验（reports/196 真实掉卡根因）：
    #   若 cur 是 0xffffffff，则 (cur & ~0xC0000)|0x40000 = 0xFFF7FFFF，
    #   bit0 = 1 = COMMIT → 必掉卡。
    #   若 cur 的 bit0 本来已是 1，写回也会保留 COMMIT → 同样掉卡。
    if cur == 0xFFFFFFFF or (cur & 1):
        print(f"  ✗ 0x8C040 = {cur:#010x} 不可信（ffffffff 或 COMMIT=1），"
              f"拒绝写 SPEED")
    elif ((cur >> 18) & 3) == GEN2_SPEED:
        print(f"  SPEED 已是 {GEN2_SPEED}（{cur:#010x}）")
    else:
        bar.wr(R_SPEED, (cur & ~0x000C0000) | (GEN2_SPEED << 18))
        print(f"  0x8C040 {cur:#010x} → 写入 {bar.rd(R_SPEED):#010x}"
              f"  SPEED={(bar.rd(R_SPEED) >> 18) & 3}")
        if not bar.alive():
            panic(bar, "⑤ 写 SPEED 后")
        print(f"  存活 ✓ LCAP2={bar.rd(R_LNKCAP2):#06x}")

    # ---------- ⑥ 根端口 Retrain ----------
    print(f"\n=== ⑥ 等 {POST_SPEED_WAIT:.0f}s，再从【根端口】Retrain（不碰端点）===")
    time.sleep(POST_SPEED_WAIT)
    ok = False
    for i in range(1, 7):
        if not bar.alive():
            panic(bar, f"⑥ 第 {i} 轮前")
        v = pci(ROOT, "CAP_EXP+0x10.w")
        if v is None:
            print(f"  第{i}轮: 根端口读不到"); break
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")
        time.sleep(1.0)
        st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
        rp = pci(ROOT, "CAP_EXP+0x12.w", tries=5)
        sp = "-" if st is None else (st & 0xF)
        print(f"  第{i}轮: 端点 LNKSTA={('N/A' if st is None else format(st, '#06x'))} "
              f"sp={sp} | 根端口={('N/A' if rp is None else format(rp, '#06x'))}")
        if st is not None and (st & 0xF) >= 2:
            ok = True
            break
    dump("⑦ 最终", bar,
         {"latched": latched, "gen2": ok})

    st = pci(DEV, "CAP_EXP+0x12.w", tries=5)
    print(f"\n=== 结果 ===  声明锁存={latched}  Gen2={ok}  "
          f"端点 sp={'-' if st is None else st & 0xF}")
    print("  " + sh(f"lspci -vv -s {DEV} 2>/dev/null | grep -E 'LnkSta:'").stdout.strip())
    bar.close()

    if ok:
        print("\n带宽实测（pinned/pageable 对照）:")
        print("  " + sh("timeout 150 /data/labs/nvidia/pcie-gen2/bwtest2 128 7 2>&1")
              .stdout.strip().replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
