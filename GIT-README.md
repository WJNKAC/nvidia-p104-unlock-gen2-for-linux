# git 仓库说明

## 现状

```
仓库根：/data/labs/nvidia/nvidia-p104-unlock-gen2-for-linux
首次提交：201b4a4  初始提交：P104-100 PCIe Gen2 解锁方案 + 历史归档
跟踪文件：312
.git 大小：3.7 M
分支：master（或 main，取决于 git 默认）
```

本地仓库，**没有配置 remote**（没有推送到任何地方）。

---

## 首次建立时做的处理

### 1. 敏感信息扫描（结果：干净）

| 检查项 | 结果 |
|---|---|
| 密码 / passwd / secret / token / API key | 无 |
| 私钥 / `id_rsa` / `.pem` / `BEGIN PRIVATE KEY` | 无 |
| 公钥（`ssh-rsa` / `ssh-ed25519`） | 无 |
| `.env` 类配置文件 | 无 |
| 公网 IP | 无（只有内网 `192.168.1.x`） |
| MAC 地址 | 无 |
| 用户名 / home 路径 | 无（已脱敏为 `user` / `/home/user`） |

### ★ 面向公开仓库的脱敏（2026-10-29）

首次提交时文档里留有内网信息，**push 到公开仓库前已统一替换**：

| 原内容 | 替换为 | 出现处 |
|---|---|---|
| 内网地址（实验机） | `〈实验机IP〉` | 6 处 |
| 内网地址（NFS 服务器） | `〈NFS服务器IP〉` | 2 处 |
| 账号@主机 | `user@<实验机IP>` | 3 处 |
| 账号的 home 路径 | `/home/user` | 1 处 |
| 实验机主机名 | `<主机名>` | 2 处 |

处理方式：对全部 74 个已跟踪的 `.md` 做 `sed` 批量替换。

**脚本没受影响** —— 确认过 `bin/*.sh` / `bin/*.py` / `kmod/*.c` 里没有硬编码 IP 或账号，
路径全部通过 `_common.sh` 的 `$PKG_ROOT` 推导。

⚠ 注意：`history/` 里的报告是历史记录，脱敏只改了地址，**技术内容一字未动**。

### 2. 移除了嵌套的 `.git`

`history/pcie-gen2/ref/` 下有两个第三方参考项目原本带 `.git`：

- `cmpunlocker/.git`
- `p104-gen2-unlock/.git`

**必须移出**，否则 `git add` 会把它们当 gitlink（submodule），
父仓库里只记一个 commit hash，**源码内容根本不入库** —— 归档就废了。

处理方式：

1. 先尝试 `git bundle create --all` 保留历史 ⇒ **生成不完整**
   （clone 报 `Could not read <commit>` / `Failed to traverse parents`）
2. 查明原因：两者都是 **shallow clone**（`--depth 1`），**只有 1 个 commit，没有历史**
3. 判定 bundle 无意义 ⇒ 放弃，只保留源码
4. 原 `.git` **移到包外暂存**（没删）：
   `/data/labs/nvidia/.nested-git-stash-20261029/`

详见 `history/pcie-gen2/ref/README.md`。

### 3. `.gitignore` 的两处特意设计

**排除**（可重建 / 每次运行都会变）：
- `build/` —— `bin/build.sh` 会重新生成
- `kmod/*.o` `kmod/*.ko` 等 —— 编译产物
- `logs/*.log` —— 但用 `!logs/复现-长跑-8min.log` 保留了复现证据

**不排除**（归档价值）：
- `history/pcie-gen2/kmod/*.ko|*.o` —— 当年参与实验的具体二进制，
  源码后来改过，**重建不出同一份**
- `history/pcie-gen2/*.log` —— 当年实验的原始记录

⚠ 忽略规则写的是 `kmod/*.o` 这种**相对路径**，只匹配顶层 `kmod/`，
不影响 `history/pcie-gen2/kmod/`。改动时注意别写成 `*.ko`（会误伤 history）。

---

## 常用操作

```bash
cd /data/labs/nvidia/nvidia-p104-unlock-gen2-for-linux

# 看状态 / 历史
git status
git log --oneline
git show --stat HEAD

# 提交改动
git add -A
git commit -m "说明"

# 看某个文件的历史
git log --follow -p docs/寄存器参考.md

# 撤销未提交的修改
git restore <文件>

# 对比工作区与上次提交
git diff
```

## 加 remote 时注意

如果要推到远程，**先确认**：

1. 目标仓库的可见性（私有 / 公开）
2. 若公开，按上面「敏感信息扫描」的表把内网 IP 与账号名替换掉
3. `history/` 里有 5.7 M 内容（含 3 个 `.ko` 二进制）——
   推到 GitHub 之类有单文件 100 M 限制（这里都远低于），但仓库会偏大

```bash
git remote add origin <url>
git push -u origin master    # 或 main
```

## 一个坑：属主

仓库建立时是以 root 身份 `git init` 的，`.git` 属主变成 `root:root`，
`union` 用户将无法提交（`fatal: unable to create '.git/index.lock': Permission denied`）。

已修：

```bash
sudo chown -R union:nogroup /data/labs/nvidia/nvidia-p104-unlock-gen2-for-linux
```

以后如果以 root 跑过 `git` 命令，记得再改回来。
