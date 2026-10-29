#!/usr/bin/env python3
"""patch_speed_lock.py —— 唯一签名补丁：解除"SPEED 跟随当前速率"的锁定环

## 背景（reports/192）

静态定位到一段独立函数（`nv-kernel.o_binary` 的 `.text+0x105cc0` 区域，
对应 `nvidia.ko` 里 `.text+0x126c4e` 同构），逻辑：

```asm
105d41: mov  ecx,0x8c040     ; 读 LINK_CONFIG_0
105d4d: call <read>
105d57: mov  ecx,0x88088     ; 读 LINK_CTRL_STATUS
105d63: call <read>
105d68: shr  eax,0x10        ; LNKSTA
105d6f: and  edx,0xf         ; edx = 当前速率 sp
105d72: and  eax,0xfff3ffff  ; 清 SPEED[19:18]
105d77: cmp  edx,0x1
105d7a: je   0x1060f0        ; ★ sp==1 → 走下面
105d80: mov  r8d,eax
105d83: or   r8d,0x40000     ; sp!=1 时：SPEED=1
105d8a: cmp  edx,0x2
105d8d: cmovne r8d,eax       ; sp!=2 时：SPEED=0
105d97: mov  ecx,0x8c040     ; 写回
105da0: call <write>
105da5: ret

; ★ sp==1 分支
1060f0: or   eax,0x80000     ; SPEED=2（Pascal: 2.5GT/s = Gen1）
1060f5: mov  r8d,eax
1060f8: jmp  0x105d91        ; 回去写
```

**这是一个自我维持的锁定环**：链路是 Gen1 → 它把速率请求也写成 Gen1 → 永不升速。

## 补丁

把 `0x1060f0` 的 `or eax,0x80000`（SPEED=2）改成 `or eax,0x40000`（SPEED=1）。

```
原: 0d 00 00 08 00   (or eax,0x80000)
新: 0d 00 00 04 00   (or eax,0x40000)
              ^ 改这一个字节
```

## ★ 为什么这次能唯一定位（上次失败的原因）

`0d 00 00 08 00` 这个 5 字节模式在全文件出现 **13812 次**（它只是 `or eax,imm32`，
任何地方都可能出现），**绝不能用来定位**。上次 1 字节补丁失败就是这个原因。

唯一的是它的**前文条件**：

```
83 fa 01              cmp edx,0x1
0f 84 70 03 00 00     je  +0x370      ← 指向 0x1060f0
```
这个 9 字节签名 **仅 1 处命中**。

## 用法

  sudo python3 patch_speed_lock.py check
  sudo python3 patch_speed_lock.py apply
  sudo python3 patch_speed_lock.py revert
"""
import hashlib
import os
import shutil
import sys

KO = "/lib/modules/%s/updates/dkms/nvidia.ko" % os.uname().release
BAK = KO + ".orig_speedlock"

# ★ 唯一签名：cmp edx,1 ; je <目标>
SIG_CMP = bytes.fromhex("83fa010f84700300")
# 目标指令：or eax,0x80000 ; mov r8d,eax ; jmp rel32
SIG_OR = bytes.fromhex("0d000008004189c0e994fcffff")
# 打补丁后的形态：or eax,0x40000 ; mov r8d,eax ; jmp rel32
SIG_OR_NEW = bytes.fromhex("0d000004004189c0e994fcffff")

PATCH_REL = 3          # SIG_OR 里第 3 字节是 0x08
ORIG = 0x08
NEW = 0x04


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
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
    act = sys.argv[1] if len(sys.argv) > 1 else "check"
    if os.geteuid() != 0:
        print("需要 root"); return 1
    if not os.path.exists(KO):
        print(f"找不到 {KO}"); return 1

    print(f"目标 {KO}")
    print(f"md5  {md5(KO)}")
    with open(KO, "rb") as f:
        data = f.read()
    print(f"大小 {len(data):,}\n")

    if act == "revert":
        if not os.path.exists(BAK):
            print(f"没有备份 {BAK}"); return 2
        shutil.copy2(BAK, KO)
        print(f"已还原，md5 {md5(KO)}")
        return 0

    # 1) 用唯一签名定位 cmp/je 对
    cmp_hits = find_all(data, SIG_CMP)
    print(f"签名 cmp edx,1; je +0x370 : 命中 {len(cmp_hits)} 处")
    if len(cmp_hits) != 1:
        print("!! 签名不唯一，拒绝打补丁")
        for h in cmp_hits[:10]:
            print(f"   {h:#x}")
        return 2
    anchor = cmp_hits[0]
    # je 相对跳转：je 指令在 anchor+3，长度 6，目标 = anchor+3+6+0x370
    or_addr = anchor + 3 + 6 + 0x370
    print(f"  anchor {anchor:#x} → 推断 sp==1 分支 {or_addr:#x}")

    # 2) 校验该处确实是预期指令（★ 必须同时接受原始形态与已打补丁形态，
    #    否则 apply 之后再 check 会误报"拒绝打补丁"—— 2026-10-29 踩过）
    got = data[or_addr:or_addr + len(SIG_OR)]
    print(f"  该处字节: {got.hex(' ')}")
    already = (got == SIG_OR_NEW)
    if got != SIG_OR and not already:
        print("!! 该处字节与预期不符，拒绝打补丁")
        return 2
    if already:
        print("  ✓ 已是补丁形态（or eax,0x40000 = SPEED=1）")
    else:
        print("  ✓ 与预期一致（or eax,0x80000; mov r8d,eax; jmp = SPEED=2）")

    cur = data[or_addr + PATCH_REL]
    if act == "check":
        if cur == ORIG:
            print(f"\n[未打补丁] 偏移 {or_addr + PATCH_REL:#x} = {cur:#04x}")
            print(f"  打补丁后 → {NEW:#04x}：or eax,0x40000 (SPEED=1, Gen2)")
        elif cur == NEW:
            print(f"\n[已打补丁] 偏移 {or_addr + PATCH_REL:#x} = {cur:#04x}")
            print("  （需重载 nvidia 模块才生效）")
        else:
            print(f"\n[异常] 字节 = {cur:#04x}")
        return 0

    if act == "apply":
        if cur == NEW:
            print("\n已打过补丁"); return 0
        if cur != ORIG:
            print(f"\n!! 字节异常 {cur:#04x}"); return 2
        if not os.path.exists(BAK):
            shutil.copy2(KO, BAK)
            print(f"\n已备份 → {BAK}")
        with open(KO, "r+b") as f:
            f.seek(or_addr + PATCH_REL)
            f.write(bytes([NEW]))
        with open(KO, "rb") as f:
            nd = f.read()
        print(f"已写入。新字节: {nd[or_addr:or_addr + len(SIG_OR)].hex(' ')}")
        print(f"新 md5: {md5(KO)}")
        return 0

    print(f"未知动作 {act}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
