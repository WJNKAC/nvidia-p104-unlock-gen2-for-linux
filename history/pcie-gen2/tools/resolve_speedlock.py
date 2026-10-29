#!/usr/bin/env python3
"""resolve_speedlock.py —— 精确解析 SPEED 锁定环位置，一次说清"三种偏移"

★ 背景（reports/193）：这里极易混淆三种偏移，我已两次搞错：

  1. **文件偏移**（file offset）：`patch_speed_lock.py` 用的就是它，
     直接对 `nvidia.ko` 读写，正确。
  2. **.text 内偏移**（section offset）：反汇编时 `objdump --start-address` 用的。
     `.text` 的 sh_offset = 0x40，所以 `file = text + 0x40`。
  3. **运行时地址**：`.text 基址 + .text 内偏移`。

  两个文件（`nvidia.ko` 157 MB / `nv-kernel.o_binary` 112 MB）的 `.text`
  布局**完全不同**，偏移绝不能互相套用：
      nvidia.ko     .text size = 15,333,236
      nv-kernel.o   .text size = 15,198,308

本脚本统一输出三种表示，并做交叉校验。

用法：python3 resolve_speedlock.py
"""
import os
import struct
import subprocess

KO = "/lib/modules/%s/updates/dkms/nvidia.ko" % os.uname().release

# 唯一签名：cmp edx,1 ; je +0x370
SIG = bytes.fromhex("83fa010f84700300")
# 目标指令
OR_INS = bytes.fromhex("0d000008004189c0e994fcffff")
JE_LEN = 6
JE_DISP = 0x370


def text_base():
    """从 sysfs 取 .text 运行时基址（需 root）"""
    try:
        with open("/sys/module/nvidia/sections/.text") as f:
            return int(f.read().strip(), 16)
    except Exception:
        return None


def main():
    with open(KO, "rb") as f:
        d = f.read()
    print(f"文件 {KO}")
    print(f"大小 {len(d):,}\n")

    # 找签名（文件偏移）
    hits, i = [], 0
    while True:
        i = d.find(SIG, i)
        if i < 0:
            break
        hits.append(i)
        i += 1
    print(f"签名 {SIG.hex()} 命中 {len(hits)} 处")
    if len(hits) != 1:
        print("!! 不唯一或未命中，中止")
        for h in hits[:10]:
            print(f"   {h:#x}")
        return 2

    anchor_file = hits[0]
    # je 在 anchor+3，长度 6
    tgt_file = anchor_file + 3 + JE_LEN + JE_DISP
    print(f"\n{'='*66}")
    print("三种偏移对照")
    print(f"{'='*66}")
    print(f"  签名 anchor      file={anchor_file:#x}   .text+{anchor_file - 0x40:#x}")
    print(f"  ★ 目标指令       file={tgt_file:#x}   .text+{tgt_file - 0x40:#x}")

    tb = text_base()
    if tb:
        print(f"  运行时地址       {tb + tgt_file - 0x40:#x}"
              f"   （.text 基址 {tb:#x} + {tgt_file - 0x40:#x}）")
    else:
        print(f"  运行时地址       （需 root 读 /sys/module/nvidia/sections/.text）")

    # 交叉校验：文件偏移处必须真的是 or eax,0x80000
    got = d[tgt_file:tgt_file + len(OR_INS)]
    print(f"\n该处字节（file {tgt_file:#x}）: {got.hex(' ')}")
    if got != OR_INS:
        print("!! 与 or eax,0x80000 不符，补丁无效")
        return 2
    print("  ✓ = or eax,0x80000 ; mov r8d,eax ; jmp rel32")

    patch_at = tgt_file + 3
    print(f"\n待改字节: file {patch_at:#x}  当前 {d[patch_at]:#04x}"
          f"  →  {0x04:#04x}（or eax,0x40000）")

    # 用正确方式反汇编验证（注意 --start-address 用 .text 内偏移）
    text_off = tgt_file - 0x40
    tmp = "/tmp/_check_speedlock.bin"
    with open(tmp, "wb") as f:
        f.write(d[0x40:0x40 + 15333236])   # 只取 .text 段
    print(f"\n反汇编验证（--start-address 用 .text 内偏移 {text_off:#x}）:")
    try:
        r = subprocess.run(
            ["objdump", "-D", "-b", "binary", "-m", "i386:x86-64",
             "-M", "intel", "--start-address", str(text_off - 0x10),
             "--stop-address", str(text_off + 0x10), tmp],
            capture_output=True, text=True, timeout=60)
        for l in r.stdout.splitlines():
            if ":\t" in l and l.strip().split(":")[0].strip():
                try:
                    a = int(l.split(":")[0].strip(), 16)
                except ValueError:
                    continue
                mark = " ★" if a == text_off else "  "
                print(f"{mark}{l.strip()}")
    except Exception as e:
        print(f"  objdump 失败: {e}")
    os.unlink(tmp)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
