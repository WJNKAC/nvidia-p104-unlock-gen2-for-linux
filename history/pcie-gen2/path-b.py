#!/usr/bin/env python3
"""path-b.py —— 通路 B：0x8841C[15] 复位前置位 + 复位锁存

参考项目 docs/04-硬件事实表.md §5.1：
    通路 B：0x8841C[15] = "L 的置位者"，
            复位前把 bit15 置 1 → 复位后声明锁存为 Gen2（他实测 7/7）

思路：不碰 0x8C040（已实测必掉卡），只写 bit15，然后让 GPU 复位重新初始化，
      由 GPU 自己读 0x8841C 生成 PCIe 能力声明并按新声明重新训练链路。

用法（需先有 iomem=relaxed）：
    sudo python3 path-b.py check     只读，看当前状态
    sudo python3 path-b.py apply     写 bit15 + 复位 + 验证
    sudo python3 path-b.py revert    只清 bit15（不改链路）

⚠ 复位会让 GPU 短暂消失。显示在核显时安全；若 GPU 正在跑任务会失败。
"""
import mmap, os, struct, subprocess, sys, time

PHYS, SIZE = 0xF6000000, 0x1000000
DEV = "0000:01:00.0"
CFG_REG = 0x8841C
BIT15 = 1 << 15

def mm():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)
    return fd, m

def state(m):
    rd = lambda o: struct.unpack_from("<I", m, o)[0]
    sta = rd(0x88088) >> 16
    return {
        "cfg":   rd(CFG_REG),
        "cap":   rd(0x88084),
        "cap2":  rd(0x880A4),
        "ctl2":  rd(0x880A8),
        "sta":   sta,
        "speed": sta & 0xF,
        "width": (sta >> 4) & 0x3F,
        "punits": rd(0x2241C),
    }

def show(tag, s):
    sp = {1: "Gen1(2.5GT/s)", 2: "Gen2(5.0GT/s)", 3: "Gen3(8.0GT/s)"}
    print(f"  [{tag}] 0x8841C={s['cfg']:#010x} bit15={(s['cfg']>>15)&1} [14:13]={(s['cfg']>>13)&3}")
    print(f"         LNKSTA={s['sta']:#06x} speed={s['speed']}({sp.get(s['speed'],'?')}) width=x{s['width']}")
    print(f"         LNKCAP2={s['cap2']:#010x}  LNKCTL2={s['ctl2']:#06x}  0x2241C={s['punits']:#010x}")

def do_check():
    fd, m = mm()
    print("=== 当前状态 ===")
    show("now", state(m))
    m.close(); os.close(fd)

def reset_gpu():
    """依次尝试三种复位方式，返回成功的方式名"""
    print("\n=== 触发复位 ===")
    # 1) unbind + bind（最可控：让驱动重新初始化设备）
    drv = "/sys/bus/pci/drivers/nvidia"
    if os.path.isdir(drv):
        try:
            print("  方式1: unbind/bind nvidia 驱动")
            open(f"{drv}/unbind", "w").write(DEV); time.sleep(2)
            open(f"{drv}/bind", "w").write(DEV); time.sleep(4)
            if os.path.exists(f"/sys/bus/pci/devices/{DEV}"):
                print("    ✓ 完成")
                return "unbind/bind"
        except Exception as e:
            print(f"    失败: {e}")
    # 2) PCI 层 reset
    try:
        print("  方式2: PCI reset")
        open(f"/sys/bus/pci/devices/{DEV}/reset", "w").write("1")
        time.sleep(4)
        return "pci reset"
    except Exception as e:
        print(f"    失败: {e}")
    # 3) nvidia-smi --gpu-reset
    try:
        print("  方式3: nvidia-smi --gpu-reset")
        r = subprocess.run(["nvidia-smi", "--gpu-reset", "-i", "0"],
                           capture_output=True, text=True, timeout=60)
        time.sleep(4)
        if r.returncode == 0:
            return "gpu-reset"
        print(f"    失败: {r.stderr.strip()[:80]}")
    except Exception as e:
        print(f"    失败: {e}")
    return None

def do_apply():
    fd, m = mm()
    before = state(m)
    print("=== 写前 ===")
    show("before", before)

    if before["speed"] == 2:
        print("\n已经是 Gen2，无需操作"); m.close(); os.close(fd); return

    new = before["cfg"] | BIT15
    print(f"\n=== 写 0x8841C: {before['cfg']:#010x} → {new:#010x}（置 bit15）===")
    struct.pack_into("<I", m, CFG_REG, new)
    time.sleep(0.3)
    rb = struct.unpack_from("<I", m, CFG_REG)[0]
    print(f"  读回 {rb:#010x}  bit15={(rb>>15)&1}")
    if not (rb >> 15) & 1:
        print("  ✗ bit15 写不进去，通路 B 不适用"); m.close(); os.close(fd); return
    m.close(); os.close(fd)      # 复位前必须释放映射

    how = reset_gpu()
    if not how:
        print("\n✗ 三种复位方式都失败，请手动重启后重试")
        return

    print(f"\n=== 复位后状态（方式：{how}）===")
    time.sleep(2)
    try:
        fd2, m2 = mm()
        after = state(m2)
        show("after", after)
        print()
        if after["speed"] == 2:
            print("  ★★★ 成功：链路已到 Gen2 ★★★")
            print("  验证：nvidia-smi --query-gpu=pcie.link.gen.current --format=csv")
        elif after["cap2"] == 0x06:
            print("  能力声明已是 Gen2（0x06），但链路速率仍 Gen1 —— 声明解锁、链路未切")
        else:
            print("  未生效：声明与速率都没变")
        m2.close(); os.close(fd2)
    except OSError as e:
        print(f"  复位后无法访问 BAR0: {e}（设备可能未回来，需重启）")

def do_revert():
    fd, m = mm()
    s = state(m)
    new = s["cfg"] & ~BIT15
    print(f"清 bit15: {s['cfg']:#010x} → {new:#010x}")
    struct.pack_into("<I", m, CFG_REG, new)
    time.sleep(0.2)
    print(f"  读回 {struct.unpack_from('<I', m, CFG_REG)[0]:#010x}")
    print("  注意：已锁存的声明需要复位才会改变")
    m.close(); os.close(fd)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("需要 root：sudo python3 path-b.py ..."); sys.exit(1)
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": do_check, "apply": do_apply, "revert": do_revert}.get(mode, do_check)()
