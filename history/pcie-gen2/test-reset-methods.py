#!/usr/bin/env python3
"""test-reset-methods.py —— 逐个测试"让设备重新初始化"的候选方法（v2）

背景（reports/180）：Linux 的 remove 会把设备置 D3cold，rescan 后没东西拉回 D0。
用户提出三个候选：
  M1  unbind → echo 0 > enable → echo 1 > enable → bind
  M2  power/control + runtime PM 显式控制
  M3  setpci 写 PM 寄存器做 D3hot → D0 循环（不离开总线）
  M4  （对照，已知可用）unbind → remove → rescan

判据（v2 重设计，比 v1 安全得多）：
  ★ 硬件默认链路是 Gen1。所以：
      1. 先把链路推到 Gen2（写 SPEED=1 + 根端口 Retrain，已实测可用）
      2. 再执行候选方法
      3. 若链路掉回 Gen1  ⇒ PCIe 核被复位了（方法有效）
         若仍是 Gen2     ⇒ 核没被动过（方法对"重新初始化"无效）
  v1 原本靠写绝对值 0xC0B4AD00 到 0x8841C 做标记，实测**会把设备写死**
  （跳过 XP3G 前置 + 绝对值覆盖），已废弃。

★ 另一个血泪教训（本轮实测）：
 写 0x8C040.SPEED 之后必须**等待 ≥0.5 秒**才能触发重训；
 毫秒级立刻重训会让端点 LTSSM 死锁（reports/180 的 hold.py 事故）。

用法：sudo python3 test-reset-methods.py [M1 M2 M3 M4]
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
R_CAP2, R_LNKCFG0 = 0x880A4, 0x8C040
GRACE = 20
SYS = f"/sys/bus/pci/devices/{DEV}"

def sh(c, t=60):
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

def alive(m):
    for _ in range(GRACE):
        if rd(m, R_CAP2) != 0xFFFFFFFF:
            return True
        time.sleep(0.02)
    return False

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None

def on_bus():
    return sh(f"lspci -Dn | grep -c '10de:1b87'").stdout.strip() not in ("", "0")

def ep_sp():
    v = pci(DEV, "CAP_EXP+0x12.w")
    return None if v is None else (v & 0xF)

def rp_sp():
    v = pci(ROOT, "CAP_EXP+0x12.w")
    return None if v is None else (v & 0xF)

def push_gen2():
    """写 SPEED=1（不发 COMMIT），等 0.8s，再从根端口重训一次"""
    try:
        fd, m = bar()
        if not alive(m):
            m.close(); os.close(fd); return False
        lk = rd(m, R_LNKCFG0)
        if ((lk >> 18) & 3) != 1:
            wr(m, R_LNKCFG0, (lk & ~0x000C0000) | (1 << 18))
        m.close(); os.close(fd)
    except Exception as e:
        print(f"    push_gen2 MMIO 失败: {e}"); return False
    time.sleep(0.8)                      # ★ 必须等，否则重训死锁
    for _ in range(3):
        v = pci(ROOT, "CAP_EXP+0x10.w")
        if v is None:
            return False
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(v | 0x20):04x}")
        time.sleep(0.8)
        if ep_sp() == 2:
            return True
    return ep_sp() == 2

def status(tag=""):
    d = {
        "on_bus": on_bus(), "power": sh(f"cat {SYS}/power_state 2>/dev/null").stdout.strip(),
        "bound": os.path.exists(f"{SYS}/driver"),
        "cfg": pci(DEV, "0.l"), "ep_sp": ep_sp(), "rp_sp": rp_sp(),
        "lcpa2": pci(DEV, "CAP_EXP+0x2c.l"),
    }
    cfgok = d["cfg"] is not None and d["cfg"] != 0xFFFFFFFF
    print(f"  {tag} bus={d['on_bus']} power={d['power']} bound={d['bound']} "
          f"cfg={'OK' if cfgok else '!!ff'} LCAP2="
          f"{'-' if d['lcpa2'] is None else format(d['lcpa2'],'#06x')} "
          f"端点sp={d['ep_sp']} 根端口sp={d['rp_sp']}")
    return d, cfgok

# ---------------- 四个候选 ----------------
def M1():
    print("  M1: unbind → enable=0 → enable=1 → bind")
    sh(f"echo {DEV} > /sys/bus/pci/drivers/nvidia/unbind"); time.sleep(1)
    sh(f"echo 0 > {SYS}/enable"); time.sleep(0.6)
    a = pci(DEV, "0.l")
    print(f"    enable=0 后: power={sh(f'cat {SYS}/power_state').stdout.strip()} "
          f"cfg={('N/A' if a is None else format(a,'#08x'))}")
    sh(f"echo 1 > {SYS}/enable"); time.sleep(1)
    b = pci(DEV, "0.l")
    print(f"    enable=1 后: power={sh(f'cat {SYS}/power_state').stdout.strip()} "
          f"cfg={('N/A' if b is None else format(b,'#08x'))}")
    sh(f"echo {DEV} > /sys/bus/pci/drivers/nvidia/bind"); time.sleep(3)
    return True

def M2():
    print("  M2: power/control auto → on（runtime PM）")
    print(f"    前: control={sh(f'cat {SYS}/power/control').stdout.strip()} "
          f"runtime={sh(f'cat {SYS}/power/runtime_status').stdout.strip()}")
    sh(f"echo auto > {SYS}/power/control"); time.sleep(2.5)
    print(f"    auto 后: runtime={sh(f'cat {SYS}/power/runtime_status').stdout.strip()} "
          f"power={sh(f'cat {SYS}/power_state').stdout.strip()}")
    sh(f"echo on > {SYS}/power/control"); time.sleep(2.5)
    print(f"    on 后:   runtime={sh(f'cat {SYS}/power/runtime_status').stdout.strip()} "
          f"power={sh(f'cat {SYS}/power_state').stdout.strip()}")
    return True

def M3():
    print("  M3: setpci PM 寄存器 D3hot → D0")
    pm = pci(DEV, "CAP_PM+4.w")
    if pm is None:
        print("    读不到 PMCSR（PM 能力可能不存在），跳过"); return False
    print(f"    PMCSR 前 = {pm:#06x}（PowerState={(pm & 3)}）")
    hot = (pm & ~0x3) | 0x3
    sh(f"setpci -s {DEV} CAP_PM+4.w={hot:04x}", t=15)
    time.sleep(2)
    a = pci(DEV, "CAP_PM+4.w"); c = pci(DEV, "0.l")
    print(f"    写 D3hot 后: PMCSR={('N/A' if a is None else format(a,'#06x'))} "
          f"cfg={('N/A' if c is None else format(c,'#08x'))} "
          f"power={sh(f'cat {SYS}/power_state').stdout.strip()}")
    d0 = hot & ~0x3
    sh(f"setpci -s {DEV} CAP_PM+4.w={d0:04x}", t=15)
    time.sleep(2)
    a = pci(DEV, "CAP_PM+4.w"); c = pci(DEV, "0.l")
    print(f"    写回 D0 后: PMCSR={('N/A' if a is None else format(a,'#06x'))} "
          f"cfg={('N/A' if c is None else format(c,'#08x'))} "
          f"power={sh(f'cat {SYS}/power_state').stdout.strip()}")
    return True

def M4():
    print("  M4（对照）: unbind → remove → rescan")
    sh(f"echo {DEV} > /sys/bus/pci/drivers/nvidia/unbind"); time.sleep(1)
    sh(f"echo 1 > {SYS}/remove")
    for _ in range(40):
        time.sleep(0.2)
        if not on_bus():
            print("    已离开总线"); break
    sh("echo 1 > /sys/bus/pci/rescan")
    for i in range(40):
        time.sleep(0.5)
        if on_bus():
            print(f"    回来了（{(i+1)*0.5:.1f}s）"); break
    sh(f"echo 1 > {SYS}/enable"); sh(f"echo on > {SYS}/power/control"); time.sleep(2)
    return True

METHODS = {"M1": M1, "M2": M2, "M3": M3, "M4": M4}

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    todo = sys.argv[1:] or ["M1", "M2", "M3"]
    print(f"将测试: {' '.join(todo)}")

    _, ok = status("开机状态")
    if not ok:
        print("\n设备不应答配置读 —— 是 C. ZOMBIE 或 B. GONE，需重启后再跑"); return 1

    if not push_gen2():
        print("\n!! 无法把链路推到 Gen2（这是方法测试的前提）")
        status("push 失败后")
        return 1
    print("  前提已建立：链路 Gen2")
    status("Gen2 基线")

    results = {}
    for name in todo:
        fn = METHODS.get(name)
        if not fn:
            print(f"未知方法 {name}"); continue
        print(f"\n{'-'*58}")
        fn()
        print(f"  方法 {name} 执行完毕，检查链路是否被复位：")
        d, cfgok = status(f"{name} 后")
        if not cfgok:
            print(f"  !!!! {name} 之后设备死了 —— 停止"); results[name] = "DEAD"; break
        reset = (d["ep_sp"] is not None and d["ep_sp"] < 2)
        results[name] = {
            "power": d["power"], "bound": d["bound"],
            "ep_sp": d["ep_sp"], "rp_sp": d["rp_sp"],
            "lcpa2": None if d["lcpa2"] is None else f"{d['lcpa2']:#06x}",
            "core_reset": reset,
        }
        print(f"  ⇒ PCIe 核{'**已复位**（链路掉回 Gen1）' if reset else '未复位（仍是 Gen2）'}")
        if reset and name != todo[-1]:
            print("  （为下一个方法重新推到 Gen2）")
            if not push_gen2():
                print("  !! 重新推 Gen2 失败"); break

    print(f"\n{'='*58}\n=== 汇总 ===")
    for k, v in results.items():
        print(f"{k}: {v}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
