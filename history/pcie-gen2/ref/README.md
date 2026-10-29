# 第三方参考项目（源码快照）

这三个项目是当初研究时的**外部参考**，不是本方案的组成部分。
放在这里是让后来者能看到"我们参考了什么"。

| 项目 | 说明 | 文件数 |
|---|---|---|
| `cmpunlocker/` | 最初的参考实现，`0x8872C=6` + `XP3G` 那套写法来自这里 | 21 |
| `p104-gen2-unlock/` | 另一个 P104 Gen2 项目（带 `docs/` 与发布包结构） | 66 |
| `P104-Gen2x4-v1.6/` + `.zip` | 一个打包发布的方案（解压 + 原件） | 16 |

---

## 关于 `.git` 目录（重要）

归档时这三个项目的 `.git` 目录**已被移出**，原因和结论如下。

### 发现
`cmpunlocker` 和 `p104-gen2-unlock` 都是 **shallow clone**（`--depth 1`）：

```
$ cat .git/shallow
...（存在）
$ git rev-list --all --count
1
```

即：**只有 1 个 commit，没有历史**。当年拿它们只是为了读代码，不是做开发。

### 处理过程
1. 先尝试用 `git bundle create --all` 保留历史 ⇒ 生成的 bundle **不完整**
   （clone 报 `Could not read <commit>` / `Failed to traverse parents`）
   —— 因为 shallow 边界让 git 认为父对象缺失，而父对象本来就不在本地。
2. 确认 shallow（各 1 个 commit）后判定 **bundle 无意义**：没有历史可保。
3. 把 `.git` 移到包外的暂存目录（**没删**）：
   `/data/labs/nvidia/.nested-git-stash-20261029/`

### 为什么必须移出
如果保留 `.git`，父仓库 `git add` 会把这个目录当成 **gitlink（submodule）**，
父仓库里只记录一个 commit hash，**源码内容根本不入库** —— 归档就废了。
（而且没有 `.gitmodules`，clone 出来是一片空白。）

### 结论
- 这三个目录现在是**纯源码快照**，内容完整（工作区文件 = 全部内容，shallow 的 1 个 commit 就是这个状态）
- 想要原始 `.git` ⇒ 在 `/data/labs/nvidia/.nested-git-stash-20261029/` 里，随时可以放回
- 想要完整历史 ⇒ 只能重新从上游 clone（本项目不提供，因为从来没拿到过）

---

## 使用这些参考代码时注意

`.gitignore` 里那些排除规则是**它们自己的**，与本仓库无关。

另外：这三个项目里有几处做法我们**已经证否**，不要照搬：

| 参考项目的做法 | 我们的实测结论 |
|---|---|
| 每 20ms 写一次 `0x8C040` COMMIT | 需要先做 PnP disable（真断电），否则危险 |
| 用用户态程序写 BAR0 | **链路动态时 2/2 打挂设备**（reports/197） |
| 写 `0x8872C=6` 触发训练 | 驱动零引用此寄存器，属无文档私有开关（reports/198） |
| 只改声明 + TLS + 重训 | 升不到 Gen2，必须同时写 SPEED（reports/186） |

详见 `../../../docs/安全纪律.md` 和 `../../../docs/实验历程.md`。
