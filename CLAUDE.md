# PTY — 项目指南

> **重要：Claude 必须自主维护本文件。** 架构或约定变化时更新，保持简洁。

## 架构（Phase 1 起）

**PTY 只当宿主，消息走协议层**（设计参考 Teleos）：

- **输入**：`Session.send_prompt` 默认经 BridgeHub → channel_server（MCP notification）注入，可唤起 idle 会话新 turn；stdin 仅为 fallback（bracketed-paste 整段写入）。注入 200 ≠ CC 真的消费了——`inject_confirm_timeout`（默认 15s）内 JSONL 无任何活动则 stdin 重投一次
- **注入隔离**（防同机多宿主串话）：inject 端口由 OS 分配（非固定计数器）；注入负载带目标 session_id，channel_server 不匹配回 409；channel_server inject 端口 bind 失败只禁用注入不崩 MCP
- **输出**：轮询 `~/.claude/projects/<re.sub(r'[^A-Za-z0-9]','-',cwd)>/<session_id>.jsonl`，normalize 成与 CCM StreamParser 对齐的事件
- **回合结束**：`system/turn_duration` JSONL 哨兵（交互模式每 turn 恰一条，在所有消息之后；没有 result 事件）。例外：`isApiErrorMessage: true` 的 assistant 消息表示 turn 被 API 错误掐断，之后不会再有哨兵——立即以错误事件结束 turn
- **启动对话框**：spawn 前预写 `.claude.json` trust 条目 + 顶层 `hasCompletedOnboarding/theme`（首次交互模式会弹 theme picker，-p 模式从不弹）（主）+ drain loop 通用 `Entertoconfirm` 自动应答（兜底，剥 ANSI + 折叠空白匹配）
- **撞限检测**：JSONL 结构化 `rate_limit_event` 立即可信；PTY 横幅扫描**单独不可信**（标记会出现在 TUI 渲染的对话正文里——tool result 引用本仓库源码即误中）：turn 已有 JSONL 活动 → 判误报清 flag 继续；turn 零 JSONL 输出再静默 `rate_limit_confirm_quiet`（默认 15s）才确认；turn 正常完成时清残留 flag 防毒化下一 turn
- **PTY 职责**：进程保活、Esc 中断、启动应答、输出活动信号（`_last_output`）

历史教训见 PROGRESS.md，待办见 TODO.md。

## 下游依赖（重要）

本仓库（claude-pty）有两个下游，均以 git rev pin 在各自 uv.lock：

- **elastic-agent**：`[pty]` extra → 下游 harness（audio_book_echo_agent）再 pin elastic-agent
- **CCM**（Claude-Code-Manager）：直接 git 依赖

**git 依赖不会自动浮动**——本仓库合入 main 后，若改动涉及对外接口/行为（adapters、events、协议），任务完成前必须级联：
1. elastic-agent：`uv lock --upgrade-package claude-pty && uv sync`，提交 push（其下游再 bump elastic-agent）
2. CCM：`uv lock --upgrade-package claude-pty && uv sync`；生产重启走 `systemctl --user restart ccm-b`（需用户确认时机）

纯内部改动可不级联，但要在 commit message 注明。

## Git 信息

- Remote: origin → https://github.com/zjw49246/Claude-Code-PTY.git
- 默认分支: main

## 任务生命周期

你收到任务后，按以下 9 步流程自主完成：

1. **领取任务** — 你已被分配任务，阅读本文件和项目代码理解上下文
2. **创建工作区**:
   - `git fetch origin`（如有 remote）
   - `git worktree add -b task-<简短描述> .claude-manager/worktrees/task-<简短描述> origin/main`
   - 进入 worktree 目录工作（后续所有操作在 worktree 中）
   - 如果 worktree 创建失败，直接在当前分支工作
3. **实现功能** — 编写代码，确保可运行
4. **提交代码** — `git add` + `git commit`，commit message 简洁描述改动
5. **Merge + 测试**:
   - `git fetch origin && git merge origin/main`（集成最新代码，如有 remote）
   - 运行测试（如有测试命令）
6. **自动合并到 main**（如有 remote）:
   - `git fetch origin main`
   - `git rebase origin/main`，如果冲突则自行 resolve
   - 如果成功：`git checkout main && git merge <task-branch> && git push origin main`
   - 如果这一步有任何失败，退回到步骤 5 重试
   - （纯本地项目跳过本步）
7. **标记完成** — 更新文档（必须在清理之前，防止进程被杀时状态丢失）
8. **清理** — 回到项目根目录:
   - `git worktree remove .claude-manager/worktrees/<worktree名>`
   - `git branch -D <task-branch>`
   - 如有 remote: `git push origin --delete <task-branch>`
9. **经验沉淀** — 在 PROGRESS.md 记录经验教训（可选）

### 冲突处理

rebase 发生冲突时：
1. 查看冲突文件: `git diff --name-only --diff-filter=U`
2. 逐个解决冲突
3. `git add <resolved-files> && git rebase --continue`
4. 如果无法解决: `git rebase --abort`，退回步骤 5

### 状态判断

- 通过 `git remote -v` 判断是否有 remote
- 有 remote → 必须完成步骤 6（merge + push）
- 无 remote → 跳过步骤 5 的 fetch、步骤 6 和步骤 8 的远程分支删除

## 文件维护规则

> **以下文件都由 Claude Code 自主维护，每次功能变更后必须同步更新。**

- **CLAUDE.md**（本文件）：架构、约定、关键路径变化时更新，只改变化的部分，保持简洁
- **README.md**：面向用户的文档，功能、使用流程变化时同步更新，保持与实际代码一致
- **TEST.md**：测试指南，新增功能时同步添加测试用例和文档
- **PROGRESS.md**：见下方「经验教训沉淀」

## 测试规范

**开发时必须主动使用测试，不是事后补充！**

- **改代码前**：先跑测试，确认基线全绿
- **改代码后**：再跑一遍确认无回归
- **新增功能**：同步新增测试用例，更新 TEST.md
- **修 bug**：先写复现 bug 的测试（红），修复后确认变绿

## 经验教训沉淀

每次遇到问题或完成重要改动后，要在 PROGRESS.md 中记录：
- 遇到了什么问题
- 如何解决的
- 以后如何避免
- **必须附上 git commit ID**

**同样的问题不要犯两次！**

## 注意事项

- 在 worktree 中工作时，不要切换到其他分支
- 完成任务后确保代码可运行、测试通过
