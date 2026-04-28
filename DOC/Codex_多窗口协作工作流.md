# Codex 多窗口协作工作流

状态：当前协作准绳
适用项目：插播 / 广告插播
维护原则：少文档、强执行；流程变了先改本文。

## 1. 目标

建立一个稳定的本地协作闭环：

- 主 Codex 窗口负责总控：理解需求、维护施工图、拆任务、验收、合并。
- 执行 Codex 窗口负责实现：在独立 worktree 中按任务包改代码、跑测试、交付报告。
- 用户负责业务决策和授权：确认优先级、复制任务包给执行窗口、确认高风险动作。

这个流程的目的不是增加形式，而是让多个 Codex 可以并行推进项目，同时避免改乱主工作区、文档和代码脱节、功能做完没人验收。

## 2. 当前前提

当前目录 `/Users/lanjinglive618/ChaBo` 是主工作区，主分支为 `main`，云端协作仓库为：

```text
https://github.com/xwangandy/chabo
```

主工作区只用于总控、验收、合并和真实 Bot 手动测试。执行窗口不得直接在主工作区开发。

如果在新机器或新目录重新开始，先建立一次版本化基线。

建议基线命令：

```bash
cd /Users/lanjinglive618/ChaBo
git init
git add .
git commit -m "baseline: chabo project scaffold"
git branch -M main
git remote add origin https://github.com/xwangandy/chabo.git
git push -u origin main
```

本机已完成初始化后，不要重复运行 `git init`。后续正常使用 `git pull --ff-only`、branch 和 worktree。

## 3. 角色边界

### 主 Codex 窗口

主窗口是项目总控，负责：

- 把用户新需求写入 `DOC/需求记录.md`。
- 判断是否需要更新 `DOC/插播_项目施工图.md`。
- 拆分任务，生成可复制给执行窗口的任务包。
- 控制并行度，避免两个执行窗口改同一批文件。
- 审查执行窗口结果，跑自动化测试。
- 在必要时使用真实 Telegram Bot 做手动验收。
- 合并通过验收的分支，清理 worktree。

主窗口原则上拥有文档和集成的最终解释权。

### 执行 Codex 窗口

执行窗口是工程执行者，负责：

- 只在主窗口指定的 worktree 和分支里工作。
- 先阅读任务包要求和相关文档。
- 只修改任务包允许的文件范围。
- 不碰真实 Bot token，不操作真实 Telegram 环境，除非任务包明确授权。
- 完成后跑任务包指定测试。
- 返回变更摘要、测试结果、风险和修改文件清单。

执行窗口不直接合并，不直接修改主工作区。

### 用户

用户负责：

- 把主窗口生成的任务包复制给执行窗口。
- 告诉主窗口执行窗口完成后的结果。
- 对涉及资金、外部发送、账号权限、删除、安装等高风险动作做最终确认。

## 4. 分支与 Worktree 命名

任务编号格式：

```text
CHB-YYYYMMDD-NN
```

分支命名：

```text
task/CHB-YYYYMMDD-NN-short-name
```

worktree 路径：

```text
/Users/lanjinglive618/ChaBo-worktrees/CHB-YYYYMMDD-NN-short-name
```

创建命令模板：

```bash
cd /Users/lanjinglive618/ChaBo
git switch main
git pull --ff-only
mkdir -p /Users/lanjinglive618/ChaBo-worktrees
git worktree add -b task/CHB-YYYYMMDD-NN-short-name \
  /Users/lanjinglive618/ChaBo-worktrees/CHB-YYYYMMDD-NN-short-name \
  main
```

执行窗口启动后进入该目录：

```bash
cd /Users/lanjinglive618/ChaBo-worktrees/CHB-YYYYMMDD-NN-short-name
```

## 5. 任务拆分规则

适合并行的任务：

- 一个任务只改 Bot 对话层，另一个只改 Admin 后台。
- 一个任务只改测试，另一个只改文档或 CLI。
- 一个任务做产品方案文档，另一个做无冲突的小工具。

不适合并行的任务：

- 多个任务同时改 `src/chabo/bot.py`。
- 多个任务同时改数据库迁移和核心服务层。
- 一个任务依赖另一个任务尚未合并的接口。
- 需求还不清楚，但直接让执行窗口大范围实现。

高冲突文件默认只能由一个窗口负责：

- `src/chabo/bot.py`
- `src/chabo/db.py`
- `src/chabo/services.py`
- `DOC/插播_项目施工图.md`
- `DOC/需求记录.md`

如果必须多人改这些文件，主窗口要先合并上游任务，再派发下一个任务。

## 6. 主窗口任务包模板

主窗口每次派发任务时，复制以下模板给执行窗口：

```text
任务编号：CHB-YYYYMMDD-NN
任务名称：
工作目录：
分支：

背景：
- 当前项目是 Telegram 频道广告插播 Bot，产品名统一为“插播”。
- 请先阅读 DOC/插播_项目施工图.md、DOC/需求记录.md、本任务相关文件。

目标：
-

允许修改：
-

禁止修改：
- 不要修改真实运行库 .runtime/
- 不要写入或泄露 Bot token、后台 token、个人账号信息
- 不要改主工作区
- 不要合并分支

实现要求：
- 遵循现有代码风格
- 新需求如影响产品规则，先更新文档再实现
- UI 文案要短，面向用户不要暴露内部字段

验收标准：
-

必须运行：
```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests
```

完成后请回复：
- 变更摘要
- 修改文件清单
- 测试命令和结果
- 未完成项或风险
```

## 7. 执行窗口完成报告模板

执行窗口完成后必须按这个格式回复：

```text
任务编号：
状态：完成 / 部分完成 / 阻塞

变更摘要：
-

修改文件：
-

验证：
- 命令：
- 结果：

风险：
-

需要主窗口确认：
-
```

## 8. 主窗口验收清单

主窗口收到执行结果后，按顺序做：

```bash
cd /Users/lanjinglive618/ChaBo
git status --short
git diff --stat main...task/CHB-YYYYMMDD-NN-short-name
git diff --check main...task/CHB-YYYYMMDD-NN-short-name
git diff main...task/CHB-YYYYMMDD-NN-short-name
```

然后进入对应 worktree 或在主仓库检出分支运行：

```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests
```

需要真实 Bot 验收时，只能由主窗口在主工作区使用 `.runtime/chabo-realtest.sqlite3` 和测试 Bot 进行，执行窗口不做真实环境测试。

验收重点：

- 是否符合施工图。
- 是否记录了新需求。
- 是否改了越界文件。
- 是否有硬编码 token、真实个人信息或本地路径泄漏。
- 是否破坏旧功能。
- 是否有迁移、账本、扣费、退款等高风险逻辑遗漏测试。

## 9. 合并流程

验收通过后：

```bash
cd /Users/lanjinglive618/ChaBo
git merge --no-ff task/CHB-YYYYMMDD-NN-short-name
python3 -m compileall -q src tests
python3 -m unittest discover -s tests
```

真实 Bot 需要重启时：

```bash
# 由主窗口按当前测试环境重启，不把 token 写入文件
```

清理 worktree：

```bash
git worktree remove /Users/lanjinglive618/ChaBo-worktrees/CHB-YYYYMMDD-NN-short-name
git branch -d task/CHB-YYYYMMDD-NN-short-name
```

如果验收失败：

- 主窗口生成返工说明。
- 执行窗口继续在同一 worktree 修复。
- 不创建新 worktree，除非原分支已经无法整理。

## 10. 并行度建议

当前阶段建议最多同时开 2 个执行窗口：

- 一个做产品/Bot 交互。
- 一个做后台/服务/测试。

等项目有稳定 git 历史、CI、更多测试覆盖后，再提高到 3-4 个窗口。

## 11. 主窗口每轮输出格式

主窗口对用户汇报时尽量保持这个结构：

```text
当前状态：
-

本轮派发：
- CHB-...

等待执行窗口返回：
-

主窗口正在做：
-

下一步：
-
```

这样用户知道哪些任务在跑、哪些已经验收、哪些还没合并。

## 12. 当前建议的第一批任务

git 基线和 GitHub 远程仓库已经建立。当前主工作区是 `/Users/lanjinglive618/ChaBo`，主分支是 `main`，远程仓库是 `https://github.com/xwangandy/chabo`。

下一批任务围绕施工图里的“频道招商金线”推进，先把最短成交路径做顺，再扩展后台和部署。

推荐顺序：

1. `CHB-001`：频道招商 deep link 首屏。
   - 目标：用户从频道里的“频道招商”按钮进入 Bot 后，第一屏直接展示“投广告到 <频道名>”、价格、广告库、广告钱包和下单入口。
   - 主要文件：`src/chabo/bot.py`、`tests/test_chabo_mvp.py`。
   - 不改履约发布格式，避免和下一任务冲突。
2. `CHB-002`：标准插播三按钮履约。
   - 目标：真实发到频道的标准插播包含“频道招商 / 查看详情 / 广告主 CTA”三按钮结构。
   - 主要文件：`src/chabo/fulfillment.py`、`src/chabo/telegram.py`、`tests/test_chabo_mvp.py`。
   - 需要兼容现有 FakeGateway 和真实 Telegram inline keyboard。
3. `CHB-003`：广告库 MVP。
   - 目标：广告主能在 Bot 内创建、查看、复用轻插播/标准插播/强插播素材。
   - 主要文件：`src/chabo/db.py`、`src/chabo/services.py`、`src/chabo/bot.py`、`tests/test_chabo_mvp.py`。
   - 做完后再把下单表单改为“选择广告库素材或新建素材”。
4. `CHB-004`：频道主自用发布工具。
   - 目标：频道主可以用插播给自己的频道发布内容/广告，并默认带平台增长入口。
   - 依赖 `CHB-002` 的三按钮能力。
5. `CHB-005`：广告钱包增强。
   - 目标：展示余额、冻结预算、每频道预算、充值入口和低余额引导。
   - 后续再接真实 Stars/人工入账完整流程。

每个执行窗口只拿一个 `CHB-*` 任务，使用独立 worktree；主窗口验收通过后再合并。
