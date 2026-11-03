#!/usr/bin/env python3
"""patch-gen2.py —— 阻止驱动在 cuInit 时把链路降回 Gen1

## 结论前置（reports/199 实测）

perf 四点断点，一次 `minictx`（1 个 CUDA 上下文）：

```
entry     0x127d60  1 次   DX=0x1                       ← mode = 1
or80000   0x127e65  1 次   AX 0x20505800                ← 清掉 SPEED[19:18]
wr_shared 0x127edd  1 次   R8=0x20585800                ← ★ SPEED=2 真的写进去了
bailret   0x1280c8  1 次
```

⇒ **SPEED=2 是软件写的，不是端点内部回写**。
  （reports/195/197 的"CPU 不可见"结论作废：那次断点是没生效。）

## 为什么改「范围检查边界」比 A/B/C 都干净

`_nv017105rm(dev, obj, mode)` 入口是一段白名单检查：

```asm
127d66: 8d 42 ff        lea  -0x1(%rdx),%eax     ; eax = mode - 1
127d81: 83 f8 02        cmp  $0x2,%eax
127d84: 0f 87 3e 03 00 00  ja 1280c8             ; (mode-1) > 2 → bail（即 mode ∉ {1,2,3}）
```

把它平移成 **`mode - 2` / `cmp $0x1`**：

```asm
127d66: 8d 42 fe        lea  -0x2(%rdx),%eax     ; eax = mode - 2
127d81: 83 f8 01        cmp  $0x1,%eax
127d84: 0f 87 ...       ja   1280c8              ; (mode-2) > 1 → bail
```

新的 bail 条件（无符号）：

| mode | mode-2 | > 1 ? | 结果 |
|---|---|---|---|
| 1 | `0xFFFFFFFF`（下溢） | 是 | **bail** ★ 唯一的行为变化 |
| 2 | 0 | 否 | 继续（Gen2） |
| 3 | 1 | 否 | 继续（Gen3） |
| ≥4 | ≥2 | 是 | bail（**与原来完全一致**） |

⇒ **唯一的行为差异就是 mode==1 从"执行"变成"bail"**，其余位、其余分支、其余 mode 全部不变。

### 为什么 bail 是安全的
- `1280c8` 是函数**唯一**的 return 点：`pop %rbx; mov %r12d,%eax; pop×4; add $0x40,%rbp; ret`
- `%r12d` 在 entry 就被 `xor %r12d,%r12d` 置 0，而 mode=1 路径里对 `%r12` 的改写
  （`lea 0x1e08(%r13),%r12`）在我们跳过的代码里
- ⇒ bail 返回 **0 = CUDA_SUCCESS**，驱动认为设置成功，不会报错、不会重试

## 与 A/B/C 的对比

| 方案 | 改什么 | 评价 |
|---|---|---|
| A 入口加 `cmp $1,%edx; je ret` | 需要 code cave | 该函数没有空隙可塞 5 字节；且要处理 CET/IBT |
| B 改 `je .mode1_case` 目标 | —— | **不存在这条指令**：mode==1 是**顺序落入**，不是跳转过来的 |
| C nop 掉 `or $0x80000` | 1 条指令 | 会落到 `movl $0x1,0xc(%rbp)`（改存储的 mode），语义不清 |
| **本方案** | **2 个字节** | 边界平移，白名单收窄，零附带影响 |

## 用法
  sudo python3 patch-gen2.py check
  sudo python3 patch-gen2.py apply
  sudo python3 patch-gen2.py revert

⚠ apply 后必须重载 nvidia 模块（或重启）才生效。
"""
import hashlib
import os
import shutil
import subprocess
import sys

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def resolve_ko():
    """找到真正的 nvidia.ko，可能是 .ko 也可能是 .ko.zst（DKMS/发行版常见）。

    顺序：TARGET_KO 环境变量 → modinfo -n nvidia → 几个常见路径。
    """
    kv = os.uname().release
    cands = []
    if os.environ.get("TARGET_KO"):
        cands.append(os.environ["TARGET_KO"])
    try:
        r = subprocess.run(["modinfo", "-n", "nvidia"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            cands.append(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    for base in ("/lib/modules/%s/updates/dkms" % kv,
                 "/lib/modules/%s/updates" % kv,
                 "/lib/modules/%s/kernel/drivers/video" % kv,
                 "/lib/modules/%s/kernel/drivers/video/nvidia" % kv):
        for name in ("nvidia.ko.zst", "nvidia.ko"):
            cands.append(os.path.join(base, name))

    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
    return None


KO = resolve_ko()
IS_ZST = False
if KO:
    with open(KO, "rb") as _f:
        IS_ZST = _f.read(4) == ZSTD_MAGIC
BAK = (KO + ".orig_nogen1") if KO else ""


def load_ko(path):
    """读入**解压后的** ELF 字节。.zst 会先 zstd -d 到内存。"""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != ZSTD_MAGIC:
        return raw
    try:
        r = subprocess.run(["zstd", "-d", "-c", "--", path], capture_output=True)
    except OSError:
        raise RuntimeError("需要 zstd 命令来解压 .ko.zst（apt install zstd）")
    if r.returncode != 0:
        raise RuntimeError("zstd 解压失败：%s" % r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout


def store_ko(path, data):
    """把**解压后**的 ELF 字节写回磁盘；原文件是 .zst 就重新压缩。

    用「写临时文件 + os.replace」保证原子性，避免中途失败留下半截 .ko。
    """
    st = os.stat(path)
    tmp = path + ".new"
    with open(path, "rb") as f:
        was_zst = f.read(4) == ZSTD_MAGIC

    if was_zst:
        level = os.environ.get("ZSTD_LEVEL", "19")
        try:
            r = subprocess.run(
                ["zstd", "-q", "-T0", "-%s" % level, "-f", "-o", tmp, "-"],
                input=data, capture_output=True)
        except OSError:
            raise RuntimeError("需要 zstd 命令来重新压缩（apt install zstd）")
        if r.returncode != 0:
            raise RuntimeError("zstd 压缩失败：%s" % r.stderr.decode("utf-8", "replace")[:200])
    else:
        with open(tmp, "wb") as f:
            f.write(data)

    os.chmod(tmp, st.st_mode)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    os.replace(tmp, path)

# ★ 唯一签名：从 entry 的 lea 到 ja 的完整 32 字节序列（全文件仅 1 处）
SIG = bytes.fromhex(
    "8d42ff"          # lea  -0x1(%rdx),%eax
    "4156"            # push %r14
    "4155"            # push %r13
    "4989fd"          # mov  %rdi,%r13
    "4154"            # push %r12
    "4531e4"          # xor  %r12d,%r12d
    "53"              # push %rbx
    "4883ed40"        # sub  $0x40,%rbp
    "48897510"        # mov  %rsi,0x10(%rbp)
    "89550c"          # mov  %edx,0xc(%rbp)
    "83f802"          # cmp  $0x2,%eax
    "0f87")           # ja   rel32

# 补丁后的形态（用于 check/重复 apply 判定）
SIG_NEW = (SIG[:2] + bytes([0xFE]) +        # lea -0x1 → lea -0x2
           SIG[3:0x1D] + bytes([0x01]) +    # cmp $0x2 → cmp $0x1
           SIG[0x1E:])

OFF_LEA = 2      # lea 的 imm8 位置（字节 ff → fe）
OFF_CMP = 0x1D   # cmp 的 imm8 位置（字节 02 → 01）

ORIG_LEA, NEW_LEA = 0xFF, 0xFE
ORIG_CMP, NEW_CMP = 0x02, 0x01


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
    if not KO:
        print("找不到 nvidia.ko（也没有 .ko.zst）")
        print("  用 TARGET_KO=/path/to/nvidia.ko 显式指定")
        return 1

    print(f"目标 {KO}")
    print(f"格式 {'zstd 压缩（.ko.zst）' if IS_ZST else '未压缩 ELF'}")
    print(f"md5  {md5(KO)}")
    try:
        data = load_ko(KO)
    except RuntimeError as e:
        print(f"!! {e}")
        return 1
    print(f"解压后 {len(data):,} 字节\n")

    if act == "revert":
        if not os.path.exists(BAK):
            print(f"没有备份 {BAK}"); return 2
        shutil.copy2(BAK, KO)
        print(f"已还原，md5 {md5(KO)}")
        if KO.endswith(".zst"):
            print("  （备份也是 .zst 格式，直接覆盖，无需再压缩）")
        return 0

    # ---- 定位 ----
    orig_hits = find_all(data, SIG)
    new_hits = find_all(data, SIG_NEW)
    total = len(orig_hits) + len(new_hits)
    print(f"签名命中：原始形态 {len(orig_hits)} 处，已补丁形态 {len(new_hits)} 处")
    if total != 1:
        print("!! 签名不唯一或找不到，拒绝操作")
        for h in orig_hits[:10]:
            print(f"   原始 @ {h:#x}")
        for h in new_hits[:10]:
            print(f"   已补丁 @ {h:#x}")
        return 2

    already = len(new_hits) == 1
    base = new_hits[0] if already else orig_hits[0]
    print(f"  定位到 {base:#x}  （对应 .text 节内偏移 {base - 0x40:#x}）")

    cur_lea, cur_cmp = data[base + OFF_LEA], data[base + OFF_CMP]
    print(f"  lea imm8 @ {base + OFF_LEA:#x} = {cur_lea:#04x}   （原 {ORIG_LEA:#04x} / 补丁 {NEW_LEA:#04x}）")
    print(f"  cmp imm8 @ {base + OFF_CMP:#x} = {cur_cmp:#04x}   （原 {ORIG_CMP:#04x} / 补丁 {NEW_CMP:#04x}）")

    if act == "check":
        if already:
            print("\n[已打补丁] mode==1 会直接 bail，驱动不再把链路降回 Gen1")
            print("  ⚠ 需重载 nvidia 模块才生效")
        else:
            print("\n[未打补丁] 当前 mode==1 会写 SPEED=2 → 链路降回 2.5GT/s")
            print("  补丁后：mode ∈ {1} → bail；{2,3} 照旧；≥4 照旧")
        return 0

    if act == "apply":
        if already:
            print("\n已打过补丁"); return 0
        if cur_lea != ORIG_LEA or cur_cmp != ORIG_CMP:
            print(f"\n!! 字节异常（lea={cur_lea:#04x} cmp={cur_cmp:#04x}），拒绝打补丁")
            return 2
        if not os.path.exists(BAK):
            shutil.copy2(KO, BAK)
            print(f"\n已备份 → {BAK}")

        nd = bytearray(data)
        nd[base + OFF_LEA] = NEW_LEA
        nd[base + OFF_CMP] = NEW_CMP
        try:
            store_ko(KO, bytes(nd))
        except RuntimeError as e:
            print(f"\n!! 写回失败：{e}")
            return 2

        print(f"已写入: {bytes(nd[base:base+len(SIG)]).hex(' ')}")
        print(f"新 md5: {md5(KO)}")
        if IS_ZST:
            print(f"  （已按 zstd -{os.environ.get('ZSTD_LEVEL', '19')} 重新压缩）")
        print("\n下一步：rmmod nvidia_uvm nvidia_modeset nvidia_drm nvidia → modprobe nvidia")
        if IS_ZST:
            print("⚠ DKMS 场景：模块已被改但 DKMS 缓存仍是旧的，")
            print("   重装内核或 dkms install 会把补丁冲掉。跑 sync-dkms.sh adopt 固化。")
        return 0

    print(f"未知动作 {act}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
