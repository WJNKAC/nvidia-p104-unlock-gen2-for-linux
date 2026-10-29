#!/bin/bash
# ⛔⛔⛔ 危险：本脚本两次把设备打成 C. ZOMBIE，不要再用 ⛔⛔⛔
#
# 事故（reports/197）：用**用户态 mmap 直接写 0x8C040**，两次在"链路刚降级"时
# 写入后立刻读回 0xffffffff，设备失联（dmesg 无 Xid），只能整机重启。
# 写入前的读值是合法的 0x20585800（bit0=0），所以不是"从垃圾值派生 COMMIT"，
# 而是"链路动态时用用户态 mmap 写 BAR0 链路控制寄存器"这条路本身不安全。
# 对照：gen2kr 的守卫用内核 writel 做同样的事，几十次按回、几十分钟，0 掉卡。
#
# ⇒ 改 BAR0 链路控制寄存器必须走内核路径。
# ⇒ 要做同类实验，请用内核模块；只读排查可用 tools/selfheal.py（单次映射 + 充分等待）。
#
# 以下原逻辑仅作记录，请勿执行。
if [ "${I_KNOW_IT_BRICKS_THE_DEVICE:-0}" != "1" ]; then
  echo "⛔ 本脚本会打死设备（实测 2/2）。"
  echo "   要做同样的事，请用内核模块，或 tools/selfheal.py 只读观察。"
  echo "   若确实要跑：I_KNOW_IT_BRICKS_THE_DEVICE=1 bash $0"
  exit 9
fi
# probe-selfheal.sh —— 降级后「只按回 SPEED、不重训」，链路会不会自己升回去？
#
# 要回答的问题（reports/196）
#   守卫的 `speed_settle_ms=3000` 占了恢复时间的 74%（3.06s / 5.65s 总时长）。
#   这个 3 秒来自 reports/186 配方「⑤ 写 SPEED → ⑥ 等 ≥3s 再 Retrain」，
#   语义是"等写 SPEED 触发的自动重训完成"。
#
#   但如果那个"自动重训"根本不发生（链路一直停在 Gen1），
#   那这 3 秒就是白等的 —— 缩短它不会带来任何风险，只会有收益。
#
#   本脚本用无风险的只读手段判定：
#     1. 确保 Gen2 → 制造一次降级
#     2. 手动把 SPEED 按回 1（守卫每轮都在做，本身安全）
#     3. 之后**不重训**，纯观察 12 秒，看 LNKSTA 是否自己变成 2
#
#   三种可能结果：
#     A. 自己升回 Gen2（例如 1–2 秒内）→ speed_settle_ms 有实际作用，可缩到该时长
#     B. 一直不升 → 这 3 秒纯属白等，可以大幅缩短（但仍需保留一个小的安全余量）
#     C. 升上去了但超过 3 秒 → 3 秒不够，需要加长（反向结论）
#
# 用法：bash probe-selfheal.sh [观察秒数]
set -u
DEV=0000:01:00.0
OBS="${1:-12}"

rd() {
python3 - "$1" <<'PY'
import mmap,os,struct,sys
off=int(sys.argv[1],16)
p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ|mmap.PROT_WRITE,offset=p)
print(struct.unpack_from('<I',m,off)[0])
PY
}
wr() {
python3 - "$1" "$2" <<'PY'
import mmap,os,struct,sys
off=int(sys.argv[1],16); val=int(sys.argv[2],16)
p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ|mmap.PROT_WRITE,offset=p)
struct.pack_into('<I',m,off,val&0xFFFFFFFF)
print(struct.unpack_from('<I',m,off)[0])
PY
}

echo "=== 0. 停掉守卫，避免两个写入者并发 ==="
# ★ 注意（reports/196）：`guard_enabled=0` 只关掉"重训"，**不关按回 SPEED**
#   —— 按回发生在 guard_enabled 检查之前（代码 ① 在 ③ 之前）。
#   所以这里必须真的卸载模块，否则会有两个 MMIO 写入者同时改 0x8C040。
if lsmod | grep -q '^gen2kr'; then
  rmmod gen2kr 2>&1 && echo "  已 rmmod gen2kr（彻底停掉守卫）" \
    || echo "  ⚠ rmmod 失败，仍有并发写入者，结果可能不可靠"
else
  echo "  gen2kr 未加载"
fi

echo
echo "=== 1. 确保当前是 Gen2 ==="
for i in $(seq 1 20); do
  L=$(rd 0x88088); SP=$(( (L >> 16) & 0xF ))
  if [ "$SP" = "2" ]; then echo "  已 Gen2（${i}s）"; break; fi
  sleep 1
done
if [ "$SP" != "2" ]; then
  echo "  当前不是 Gen2，先跑一次 push 序列"
  python3 /data/labs/nvidia/pcie-gen2/tools/unlock-decl.py --retrain >/dev/null 2>&1
  L=$(rd 0x88088); SP=$(( (L >> 16) & 0xF ))
  echo "  现在 LNKSTA[3:0]=$SP"
  [ "$SP" != "2" ] && { echo "✗ 到不了 Gen2，终止"; exit 2; }
fi

echo
echo "=== 2. 制造降级（新建 1 个 CUDA 上下文）==="
/data/labs/nvidia/pcie-gen2/minictx 1 >/dev/null 2>&1
sleep 0.5
V=$(rd 0x8C040); L=$(rd 0x88088)
printf "  降级后: 0x8C040=0x%08x SPEED=%d  LNKSTA[3:0]=%d\n" \
  "$V" $(( (V >> 18) & 3 )) $(( (L >> 16) & 0xF ))

echo
echo "=== 3. 只按回 SPEED=1（不重训），观察 ${OBS}s ==="
# ★★★ 必须先校验读值（reports/196 真实掉卡根因）：
#   若 $V 是 0xffffffff，(V & ~0xC0000)|0x40000 = 0xFFF7FFFF，bit0=1=COMMIT → 必掉卡。
#   若 $V 的 bit0 已是 1，写回同样带 COMMIT。
if [ "$V" = "4294967295" ] || [ $(( V & 1 )) -ne 0 ]; then
  echo "  ✗ 0x8C040 读值不可信（$V），拒绝写 SPEED。"
  echo "    这正是 reports/196 记录过的掉卡路径，必须拒绝。"
  echo
  echo "=== 恢复守卫 ==="
  if [ ! -e /sys/module/gen2kr ]; then
    cd /data/labs/nvidia/pcie-gen2/kmod && insmod gen2kr.ko 2>&1 \
      && echo "  已重新加载 gen2kr" || echo "  ⚠ 重新加载失败"
  fi
  exit 3
fi
NEW=$(python3 -c "print('%x' % (($V & ~0x000C0000) | (1 << 18)))")
R=$(wr 0x8C040 "$NEW")
printf "  按回后: 0x8C040=0x%08x SPEED=%d\n" "$R" $(( (R >> 18) & 3 ))

HEALED=0
for i in $(seq 1 $((OBS * 5))); do
  sleep 0.2
  L=$(rd 0x88088); SP=$(( (L >> 16) & 0xF ))
  V=$(rd 0x8C040); SD=$(( (V >> 18) & 3 ))
  T=$(python3 -c "print(f'{$i*0.2:.1f}')")
  if [ "$SP" = "2" ]; then
    echo "  ★ ${T}s 自己升回 Gen2（SPEED=$SD）"
    HEALED=1
    break
  fi
  if [ $((i % 10)) -eq 0 ]; then
    echo "  ${T}s  sp=$SP SPEED=$SD"
  fi
done

echo
echo "=== 结论 ==="
if [ "$HEALED" = "1" ]; then
  echo "  A. 自己升回了 ⇒ speed_settle_ms=3000 有实际作用（等自动重训）"
  echo "     可缩到实测恢复时长 + 安全余量"
else
  echo "  B. ${OBS}s 内没有自己升 ⇒ 这 3 秒是白等，speed_settle_ms 可大幅缩短"
  echo "     （链路只能靠显式 Retrain 恢复；仍需保留小余量避免与写 SPEED 冲突）"
fi

echo
echo "=== 恢复守卫 ==="
if [ ! -e /sys/module/gen2kr ]; then
  cd /data/labs/nvidia/pcie-gen2/kmod && insmod gen2kr.ko 2>&1 \
    && echo "  已重新加载 gen2kr" || echo "  ⚠ 重新加载失败"
fi
