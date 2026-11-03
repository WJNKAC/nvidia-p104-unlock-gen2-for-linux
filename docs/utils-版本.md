# utils 版本记录

公共库按「新功能叠版本号，纯重构不叠」管理。旧版本在没有引用后删除，
但在此**保留一行空位**，新版本号继续往上叠，**不复用旧号**。

## 命名规则

| 文件 | 函数 |
|---|---|
| `utils/bdf_v1.sh` | `bdf_discover_v1()` |
| `utils/lib_v1.sh` | `read_regs_v1()` / `is_stuck_v1()` … |
| `utils/patch_v1.py` | —（脚本，无导出函数） |

- **新增选项 / 新增功能 / 行为变化** → 叠版本号
- **纯重构（不改行为）** → 原地改，不叠

## 记录

| 版本 | 状态 | 说明 |
|---|---|---|
| `bdf_v1` | **在用** | 目标 GPU 的 PCIe BDF 自动发现。支持 `TARGET_IDS` / `TARGET_BDF`；0 张或多张一律报错退出 3（不猜，宁可停也不动错卡）。 |
| `lib_v1` | **在用** | 合并了原先重复的 `bin/_common.sh` 与 `drivers/<ver>/utils/_lib.sh`。路径、颜色、寄存器读取、`is_stuck`（连续 3 次观测）、GPU 占用检查、gen2kr 编译。 |
| `patch_v1` | **在用** | 通用二进制补丁器（JSON 规格驱动）。支持 `.ko.zst`（zstd 解压/重压，原子替换）。判定只看签名命中，md5 仅提示。 |
| ~~`*_v0`~~ | 已删除 | 占位：最初没有版本号的那三份（`bin/_common.sh`、`drivers/<ver>/utils/_lib.sh`、`drivers/<ver>/utils/_patch.py`），已合并为 v1 后删除。 |
