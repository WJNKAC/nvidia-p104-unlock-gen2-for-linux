# 分析用的 nvidia.ko 副本（未归档大文件）

归档时**没有**把 `ko-local/nvidia-6.12.113.ko`（150 MB）复制进来，原因：

- 它是 `/lib/modules/$(uname -r)/updates/dkms/nvidia.ko` 的副本，**随时可以从系统重建**
- 体积占归档的 20 倍，且内容完全相同
- 补丁工具（`bin/patch_no_gen1.py`）操作的是 `/lib/modules/...` 那份，不是这个副本

## 需要时怎么重建

```bash
cd /data/labs/nvidia/nvidia-p104-unlock-gen2-for-linux
mkdir -p history/ko-local
cp /lib/modules/$(uname -r)/updates/dkms/nvidia.ko history/ko-local/
```

## 归档时的版本信息（供核对）

| 项 | 值 |
|---|---|
| 路径 | `/data/labs/nvidia/ko-local/nvidia-6.12.113.ko` |
| 大小 | 157,271,160 字节 |
| md5（原始） | `7f6805ed15f1ae8122aa78e65a5d48a0` |
| md5（打补丁后） | `514de9d4fdd7c407a088f1baf4b62f04` |
| 内核 | 6.12.113+deb13-amd64 |
| 驱动 | NVRM 580.178.04 |

核对方法：

```bash
md5sum /lib/modules/$(uname -r)/updates/dkms/nvidia.ko
# 7f6805ed15f1ae8122aa78e65a5d48a0 → 未打补丁
# 514de9d4fdd7c407a088f1baf4b62f04 → 已打补丁
```

## 关键偏移（6.12.113 上）

| 符号 | `.text` 节内偏移 | 说明 |
|---|---|---|
| `_nv017105rm` | `0x127d60` | 驱动的 `set_link_speed(mode)` |
| `_nv017281rm` | `0x1269f0` | 另一个写 SPEED 的函数（补丁无效，见 reports/195） |
| **补丁点** | `0x127d66` / `0x127d81` | `lea` 的 imm8 / `cmp` 的 imm8 |
| 文件偏移 | **节内偏移 + 0x40** | 容易搞错，踩过两次 |
