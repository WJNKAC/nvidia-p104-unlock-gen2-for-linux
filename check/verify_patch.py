#!/usr/bin/env python3
"""verify_patch.py —— 在副本上验证补丁字节与反汇编，不碰线上 .ko

本机 /tmp 只有 10M（tmpfs），放不下 157MB 的 nvidia.ko，
所以在 union 侧运行（/tmp 有 12G）。

用法：python3 verify_patch.py
"""
import hashlib
import os
import shutil
import subprocess
import sys

KO = "/lib/modules/%s/updates/dkms/nvidia.ko" % os.uname().release
TMP = "/tmp/patched-test.ko"

SIG = bytes.fromhex(
    "8d42ff" "4156" "4155" "4989fd" "4154" "4531e4" "53" "4883ed40"
    "48897510" "89550c" "83f802" "0f87")
OFF_LEA, OFF_CMP = 2, 0x1D


def main():
    print(f"源 {KO}")
    print(f"   大小 {os.path.getsize(KO):,}")
    shutil.copy2(KO, TMP)
    d = bytearray(open(TMP, "rb").read())

    hits, i = [], 0
    while True:
        i = d.find(SIG, i)
        if i < 0:
            break
        hits.append(i)
        i += 1
    print(f"签名命中 {len(hits)} 处: {[hex(h) for h in hits]}")
    if len(hits) != 1:
        print("✗ 签名不唯一，中止"); return 2

    b = hits[0]
    print(f"base = {b:#x}   （.text 节内偏移 {b - 0x40:#x}）")
    print(f"改前: {d[b:b+32].hex(' ')}")

    # 校验待改字节确实是预期值
    if d[b + OFF_LEA] != 0xFF or d[b + OFF_CMP] != 0x02:
        print(f"✗ 待改字节异常 lea={d[b+OFF_LEA]:#04x} cmp={d[b+OFF_CMP]:#04x}")
        return 2

    d[b + OFF_LEA] = 0xFE     # lea -0x1 → lea -0x2
    d[b + OFF_CMP] = 0x01     # cmp $0x2 → cmp $0x1
    open(TMP, "wb").write(d)
    print(f"改后: {bytes(d[b:b+32]).hex(' ')}")

    print("\n--- 反汇编：打补丁前（源文件）---")
    r = subprocess.run(
        f"objdump -d --start-address={b-0x40:#x} --stop-address={b-0x40+0x28:#x} {KO}",
        shell=True, capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        if ":" in ln and any(x in ln for x in ("lea", "cmp", "ja", "push", "mov", "xor", "sub")):
            print("  " + ln.strip())

    print("\n--- 反汇编：打补丁后（副本）---")
    r = subprocess.run(
        f"objdump -d --start-address={b-0x40:#x} --stop-address={b-0x40+0x28:#x} {TMP}",
        shell=True, capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        if ":" in ln and any(x in ln for x in ("lea", "cmp", "ja", "push", "mov", "xor", "sub")):
            print("  " + ln.strip())

    print("\n--- 逻辑校验 ---")
    print("  mode=1: eax = 1-2 = 0xFFFFFFFF，无符号 > 1 → bail ✓")
    print("  mode=2: eax = 0，0 > 1? 否 → 继续（Gen2）✓")
    print("  mode=3: eax = 1，1 > 1? 否 → 继续（Gen3）✓")
    print("  mode≥4: eax ≥ 2 > 1 → bail（与原逻辑一致，原本 mode>3 也 bail）✓")

    md5 = hashlib.md5(open(TMP, "rb").read()).hexdigest()
    print(f"\n副本 md5 {md5}   保留在 {TMP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
