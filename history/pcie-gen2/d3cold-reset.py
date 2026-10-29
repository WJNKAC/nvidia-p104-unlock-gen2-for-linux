#!/usr/bin/env python3
"""d3cold-reset.py —— 让 GPU 真正断电再上电（这才是"重新初始化"）

为什么需要它（reports/181）
--------------------------
我们一直把 D3cold 当灾难，一遇到就绕开（加 unbind 防它）。
但反推下来，D3cold 可能是**唯一能让 GPU 真正断电**的路径 ——
而只有真断电，GPU 才会重新 POST、重新计算 PCIe 声明、重新配置内部数据通路参数。

现状证据链：
  · 我们写 0x8841C 只是改"声明镜像"，链路层能协商 5GT/s
  · 但真实 TLP 一来就 Xid 79 掉线，且**零 AER**
  · ⇒ GPU 内部数据面（MPSS / VC credits / Replay buffer）仍按 Gen1 配置
  · Linux 四种"复位"方法实测寄存器逐字节不变 ⇒ 都没真复位

参考项目在 Windows 上靠 DICS_DISABLE（设备进 D3cold，真断电）→ DICS_ENABLE 重新上电
实现了真复位，之后驱动在 Gen2 下正常加载、正常工作（1.55 GB/s）。

两种路径
--------
  A  unbind → power/control=auto → 等运行时挂起（自然进 D3cold）→ 唤醒
     可逆、温和；但需要设备真的允许 runtime suspend
  B  驱动绑着 → 直接 remove（驱动 remove 回调会把设备置 D3cold）→ rescan → bind
     强制、接近 Windows 的 DICS_DISABLE/ENABLE 语义

判据
----
  ★ 真断电成功 = 重上电后 BAR0 寄存器回到**硬件默认值**
      0x8841C 默认 0xc0b40500（我们改过就是 0xc0b4ad00）
      0x8C040 默认 0x20085800
  ★ 若声明 LCAP2 自己变成 0x0006 ⇒ 说明 GPU 重 POST 时读了 RMPcieLinkSpeed=1 策略！
     这就是我们要的结果：驱动从头就在 Gen2 上工作

用法：sudo python3 d3cold-reset.py [A|B]
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CAP2, R_CONFIG, R_LNKCFG0 = 0x880A4, 0x8841C, 0x8C040
DEF_CONFIG, DEF_LNKCFG0 = 0xC0B40500, 0x20085800
GRACE = 20
SYS = f"/sys/bus/pci/devices/{DEV}"
DRV = "/sys/bus/pci/drivers/nvidia"

def sh(c, t=90):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(c, 124, "", "TIMEOUT")

def rd(m, o): return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v): struct.pack_into("<I", m, o, v)

def bar():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None

def on_bus():
    return sh(f"lspci -Dn | grep -c '10de:1b87'").stdout.strip() not in ("", "0")

def peek():
    """返回 BAR0 关键寄存器；不可读返回 None"""
    try:
        fd, m = bar()
        ok = False
        for _ in range(GRACE):
            if rd(m, R_CAP2) != 0xFFFFFFFF:
                ok = True; break
            time.sleep(0.02)
        d = None
        if ok:
            d = {"cap2": rd(m, R_CAP2), "config": rd(m, R_CONFIG),
                 "lnkcfg0": rd(m, R_LNKCFG0)}
        m.close(); os.close(fd)
        return d
    except Exception:
        return None

def show(tag):
    d = peek()
    pwr = sh(f"cat {SYS}/power_state 2>/dev/null").stdout.strip()
    rt = sh(f"cat {SYS}/power/runtime_status 2>/dev/null").stdout.strip()
    cfg = pci(DEV, "0.l")
    l2 = pci(DEV, "CAP_EXP+0x2c.l")
    sp = pci(DEV, "CAP_EXP+0x12.w")
    print(f"  [{tag}] bus={on_bus()} power={pwr} runtime={rt} "
          f"bound={os.path.exists(f'{SYS}/driver')}")
    print(f"         cfg={('N/A' if cfg is None else format(cfg,'#010x'))} "
          f"LCAP2={('N/A' if l2 is None else format(l2,'#06x'))} "
          f"sp={('-' if sp is None else sp & 0xF)}"
          + (f"  BAR0: config={d['config']:#010x} lnkcfg0={d['lnkcfg0']:#010x}"
             if d else "  BAR0: 不可读"))
    return d

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    mode = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()

    print("=== 起点 ===")
    base = show("起点")
    if base is None:
        print("设备 MMIO 不可读，先重启"); return 1
    already_default = (base["config"] == DEF_CONFIG and base["lnkcfg0"] == DEF_LNKCFG0)
    print(f"  寄存器是否为硬件默认值: {already_default}"
          f"（是的话没法用'回默认'判据，需要先写入可识别值）")

    if already_default:
        print("\n=== 写入可识别标记（走正常解锁流程，实测安全的部分）===")
        fd, m = bar()
        for off, val in [(0x8E110, 1), (0x8E11C, 4), (0x8E120, 0), (0x8E12C, 0x200000)]:
            wr(m, off, val)
        c = rd(m, R_CONFIG)                      # ★ 读值再 OR，绝不写绝对值
        wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
        g = rd(m, 0x88610)
        wr(m, 0x88610, (g & ~(1 << 12)) | 1)
        wr(m, 0x8872C, 6)
        print(f"  0x8841C → {rd(m,R_CONFIG):#010x}   LINK_CAP2 = {rd(m,R_CAP2):#06x}")
        m.close(); os.close(fd)
        show("标记后")

    if mode == "A":
        print("\n=== 路径 A：unbind → control=auto → 等运行时挂起 ===")
        sh(f"echo {DEV} > {DRV}/unbind"); time.sleep(1)
        print(f"  unbind 后 bound={os.path.exists(f'{SYS}/driver')}")
        sh(f"echo auto > {SYS}/power/control")
        for i in range(10):
            time.sleep(1)
            rt = sh(f"cat {SYS}/power/runtime_status").stdout.strip()
            pw = sh(f"cat {SYS}/power_state").stdout.strip()
            print(f"  [{(i+1)}s] runtime={rt} power={pw}")
            if pw == "D3cold":
                print("  ★ 进入 D3cold —— 真断电了")
                break
        print("\n  唤醒：control=on")
        sh(f"echo on > {SYS}/power/control"); time.sleep(2)
        show("唤醒后")
        print("\n  重新 bind 驱动")
        sh(f"echo {DEV} > {DRV}/bind"); time.sleep(5)
        show("bind 后")

    elif mode == "B":
        print("\n=== 路径 B：驱动绑着直接 remove（强制 D3cold）→ rescan → bind ===")
        if not os.path.exists(f"{SYS}/driver"):
            print("  先 bind 驱动，让 remove 回调把它置 D3cold")
            sh(f"echo {DEV} > {DRV}/bind"); time.sleep(4)
        print("  remove（不 unbind）…")
        r = sh(f"echo 1 > {SYS}/remove")
        print(f"  remove 返回值={r.returncode} err={r.stderr.strip()[:80]}")
        for _ in range(30):
            time.sleep(0.2)
            if not on_bus():
                print("  已离开总线"); break
        print("  rescan …")
        sh("echo 1 > /sys/bus/pci/rescan")
        for i in range(20):
            time.sleep(0.5)
            if on_bus():
                print(f"  枚举回来（{(i+1)*0.5:.1f}s）"); break
        show("rescan 后")
        print("\n  ★ 关键：bind 驱动，让 probe 走 ACPI _PS0 上电 + GPU 完整 POST")
        r = sh(f"echo {DEV} > {DRV}/bind")
        print(f"  bind 返回值={r.returncode} err={r.stderr.strip()[:120]}")
        time.sleep(8)
        show("bind 后")
        print(f"  smi: {sh('nvidia-smi -L 2>&1 | head -1').stdout.strip()}")
        print(f"  NVRM: {sh('dmesg | grep -i NVRM | tail -3').stdout.strip()}")
    else:
        print(f"未知模式 {mode}"); return 1

    print("\n=== 结果判定 ===")
    d = show("最终")
    if d is None:
        print("  !! 设备 MMIO 不可读（D3cold 没拉回来，需重启）"); return 2
    reset = (d["config"] == DEF_CONFIG and d["lnkcfg0"] == DEF_LNKCFG0)
    l2 = pci(DEV, "CAP_EXP+0x2c.l")
    print(f"  BAR0 回硬件默认值（=真复位）: {reset}")
    print(f"  声明 LCAP2 = {('N/A' if l2 is None else format(l2,'#06x'))}"
          + ("  ← ★ GPU 重 POST 后自己声明了 Gen2！" if l2 is not None and (l2 & 6) == 6
             else "  （仍是 Gen1 声明）"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
