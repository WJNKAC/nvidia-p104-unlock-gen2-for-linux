#!/usr/bin/env python3
"""patch_nvko.py —— 给 nvidia.ko 打 1 字节补丁：把"跟随当前速率"的 Gen1 分支改成 Gen2

## 依据（reports/185，静态 + 动态双向确认）

在 nvidia.ko 的 `.text+0x126c50` 附近有一段例程，逻辑是：
    读 BAR0 0x8C040 (LINK_CONFIG_0)
    读 BAR0 0x88088 (LINK_CTRL_STATUS) → 取高 16 位 = LNKSTA → 当前速率 sp
    清 SPEED[19:18]
    sp == 1  →  or eax, 0x80000   → SPEED = 2    （Pascal: 2 = 2.5GT/s = Gen1）
    sp == 2  →  or r8d, 0x40000   → SPEED = 1    （Pascal: 1 = 5.0GT/s = Gen2）
    sp == 3  →  保持               → SPEED = 0    （8GT/s）
    写回 0x8C040

即：**它把"当前已协商速率"回写成"厂商速率请求"**，是一个自我一致的锁定环。
冷启动时链路是 Gen1 → 它写 SPEED=2 → 永远停在 Gen1。

## 补丁

把 sp==1 分支的 `or eax, 0x80000` 改成 `or eax, 0x40000`（即 SPEED=1 = Gen2）。
**只改 1 个字节**：指令 `0d 00 00 08 00` 的第 4 字节 0x08 → 0x04。

效果：当驱动认为"链路是 Gen1"时，它反而请求 Gen2，配合声明解锁（LCAP2=0x06），
链路会自己爬回 Gen2，并且**由驱动自己维持**（不再需要用户态/kretprobe 的守卫循环）。

## 用法

  sudo python3 patch_nvko.py check     # 只检查，看目标字节是否符合预期
  sudo python3 patch_nvko.py apply     # 打补丁（自动备份 .orig）
  sudo python3 patch_nvko.py revert    # 还原
"""
import hashlib
import os
import shutil
import struct
import sys

KO = "/lib/modules/%s/updates/dkms/nvidia.ko" % os.uname().release
BAK = KO + ".orig"

# 目标指令的完整签名（用于唯一定位，避免硬编码偏移）
#   0d 00 00 08 00        or eax,0x80000      ← 要改的是第 4 字节
#   41 89 c0              mov r8d,eax
#   e9 94 fc ff ff        jmp  0x126ca1
SIG = bytes.fromhex("0d00000800" "4189c0" "e994fcffff")
PATCH_AT = 3          # SIG 里 0x08 的位置
ORIG_BYTE = 0x08
NEW_BYTE = 0x04


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_all(data, pat):
    out, i = [], 0
    while True:
        i = data.find(pat, i)
        if i < 0:
            return out
        out.append(i)
        i += 1


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if os.geteuid() != 0:
        print("需要 root")
        return 1
    if not os.path.exists(KO):
        print(f"找不到 {KO}")
        return 1

    print(f"目标: {KO}")
    print(f"md5 : {md5(KO)}")
    with open(KO, "rb") as f:
        data = f.read()
    print(f"大小: {len(data):,}")

    # ★ revert 只依赖备份，不检查签名（补丁后原始签名已不存在）
    if action == "revert":
        if not os.path.exists(BAK):
            print(f"\n没有备份 {BAK}，无法还原")
            return 2
        shutil.copy2(BAK, KO)
        print(f"\n已从 {BAK} 还原")
        print(f"md5: {md5(KO)}")
        return 0

    hits = find_all(data, SIG)
    print(f"\n签名 {SIG.hex()} 命中 {len(hits)} 处")
    if len(hits) != 1:
        print("!! 签名不唯一或未找到，拒绝打补丁")
        for h in hits[:10]:
            print(f"   {h:#x}")
        return 2
    off = hits[0]
    print(f"  唯一命中: 文件偏移 {off:#x}")
    print(f"  当前字节: {data[off:off+len(SIG)].hex(' ')}")

    cur = data[off + PATCH_AT]
    if action == "check":
        if cur == ORIG_BYTE:
            print(f"\n[未打补丁] 第 {PATCH_AT} 字节 = {cur:#04x}（原始值）")
            print(f"  打补丁后会变成 {NEW_BYTE:#04x}：or eax,0x40000 → SPEED=1 (Gen2)")
        elif cur == NEW_BYTE:
            print(f"\n[已打补丁] 第 {PATCH_AT} 字节 = {cur:#04x}")
        else:
            print(f"\n[异常] 第 {PATCH_AT} 字节 = {cur:#04x}，既不是 {ORIG_BYTE:#x} 也不是 {NEW_BYTE:#x}")
        return 0

    if action == "apply":
        if cur == NEW_BYTE:
            print("\n已经打过补丁，无需重复")
            return 0
        if cur != ORIG_BYTE:
            print(f"\n!! 字节异常（{cur:#04x}），拒绝修改")
            return 2
        if not os.path.exists(BAK):
            shutil.copy2(KO, BAK)
            print(f"\n已备份 → {BAK}")
        else:
            print(f"\n备份已存在: {BAK}")
        with open(KO, "r+b") as f:
            f.seek(off + PATCH_AT)
            f.write(bytes([NEW_BYTE]))
        with open(KO, "rb") as f:
            nd = f.read()
        print(f"已写入。新字节: {nd[off:off+len(SIG)].hex(' ')}")
        print(f"新 md5: {md5(KO)}")
        # 反汇编复核
        print("\n反汇编复核（file+off 处 16 字节）:")
        print("  " + nd[off:off+16].hex(" "))
        print("  期望开头 0d 00 00 04 00 = or eax,0x40000")
        return 0

    if action == "revert":
        if not os.path.exists(BAK):
            print(f"\n没有备份 {BAK}，无法还原")
            return 2
        shutil.copy2(BAK, KO)
        print(f"\n已从 {BAK} 还原")
        print(f"md5: {md5(KO)}")
        return 0

    print(f"未知动作 {action}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
