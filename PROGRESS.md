# PROGRESS — 经验教训沉淀

## 2026-06-18 三个 PTY 模式 bug 的分析与修复

### Bug 1: CC Auto Compact 导致上下文丢失（task 660 / ccm-zhoujunwei）

**现象**: PTY 模式下 task "没有上下文"，关掉 PTY 后恢复正常。

**根因**: Claude Code 内置了 auto compact 机制。PTY 模式下 CC 进程**跨 turn 持久存活**，context 持续累积。task 660 累积到 224,838 tokens 后，CC 在 turn 之间（用户没发消息、session 空闲时）自动触发了 compact，把历史压成摘要。

非 PTY 模式下每个 turn 是独立的 `claude -p` 进程，进程退出即释放，CC 没机会在 turn 之间 auto compact。

**CC auto compact 触发条件**（从 CC 二进制逆向分析）:

- 核心判断: `if (currentTokens > autoCompactThreshold) { compact() }`
- `autoCompactThreshold` 由 `wv8(model, autoCompactWindow)` 函数计算
- `autoCompactWindow` 可配置，范围 100K-1M
- 可通过环境变量 `DISABLE_AUTO_COMPACT=true` 禁用
- 有多种 compact 类型: `compact_auto`、`compact_reactive`、`compact_manual`、`compact_partial`
- 有防抖机制: `compact_auto_rapid_refill_breaker`

**为什么 CCM 的 90% 检查没先触发**: CCM 的检查在 `_process_queued_message()` 中——只有用户发新消息时才检查。CC 的 auto compact 在空闲期触发，CCM 根本没机会介入。而且 task 660 是 1M 模型，224K 只占 22.5%，远没到 90%。

**修复**: `commit cf14e98`，PTY `_env.py` 中设 `DISABLE_AUTO_COMPACT=true`。

---

### Bug 2: CCM Compact 误判 1M 模型窗口大小（task 700 / ccm-xiaoyu）

**现象**: Task session 被意外切换，context 显示 utilization 3863%。

**根因**: CCM compact 检查中 `context_window` 值来自 CC 事件上报，但 CC 对 1M 模型可能上报 200K。

```python
window = usage.get("context_window") or 200_000  # CC 上报的可能就是 200K
```

220K tokens / 200K window = 110%（触发 compact），而实际应该是 220K / 1M = 22%。

CCM 的 `_process_event` 中已有 `_model_context_window()` 兜底（检测 `[1m]` 返回 1M），但只在**写入**时生效。compact **读取判断**时没做同样兜底，直接用了可能已错误的存储值。

**修复**: `commit fe36e0d`，compact 检查时也用模型名兜底窗口大小。

---

### Bug 3: Chat 消息重放——回复完又处理第一条消息（task 633 / ccm-xiaoyu, task 92 / 本机）

**现象**: 给 task 发消息后 Claude 先正确回复，然后又开始执行 task 的原始 description。通常在 PTY 刚开启或中断重启时出现。

**根因**: PTY chat 消息处理流程:

1. `send_prompt("用户消息")` 注入，CC 处理，turn 结束（`result` 事件）
2. `_consume` 协程完成，调用 `on_exit`
3. **CC 进程还活着**（PTY session 持久），处于交互模式等待输入
4. CC 因 session 状态（JSONL 中的 `last-prompt`、stale session 重建后的初始 description）开始自主处理旧 prompt
5. idle watcher 检测到 JSONL 增长 → `on_autonomous_event` 回调 → 事件写入 log_entries → 前端显示重放

该机制本来是为 Monitor/background agent 设计的（CC 收到 `<task-notification>` 后自主处理），但 chat 场景下意外转发了 CC 重放旧 prompt 的事件。

**为什么 CC 会重放旧 prompt**:
- JSONL 被 compact/换号/重建后，CC `--resume` 加载到不完整的上下文
- JSONL 中的 `last-prompt` 条目保存了之前的 prompt（可能是 task description）
- CC 交互模式 `--resume` 时，处理完 inject 消息后检查到未完成的工作，自动继续

**修复**: `commit 6fff435`，chat turn 的 `on_exit` 中将 `session.on_autonomous_event` 设为 `None`，阻止后续自主 turn 事件被转发。下次 launch 时 callback 重新绑定。

**Commits**: cf14e98, fe36e0d, 6fff435

---

## 2026-06-12 限流横幅误报连环冻结三个健康账号（CCM task 81/82）

### 问题：会话讨论 rate limit / 读本仓库源码就被判"撞限"

- **现象**：CCM 生产 task 81（调查 PTY）和 task 82（做撞限熔断）反复以 "usage limit reached (detected in PTY session)" 失败、exit_code=1，CCM rotation 把三个额度健康的账号全部误冻（用户手动解冻）。
- **根因**：drain loop 的横幅扫描对**所有 PTY 输出**（剥 ANSI+折叠空白+小写）滚动匹配 `usagelimitreached` 等标记，不区分 CC 真横幅与 TUI 渲染的**对话正文**。task 81/82 的 tool result 里 Read 了 `pty_process.py`/account pool 源码（标记字符串全在里面）、讨论中也满是 limit 字样 → 误中。且 `rate_limited` 是进程级 sticky flag（只在 spawn 重置），误中一次后该进程每个 turn 都被掐死 → CCM 按错误文本冻号换号 → 新号继续同一会话再误中 → 连环冻。三个账号的 JSONL 验证：0 条结构化 `rate_limit_event`，所有标记命中均为对话正文。
- **解决**（3434cd3）：横幅信号单独不可信，需 JSONL 活动交叉验证——turn 已有 JSONL 消息流动 → 判误报清 flag 继续；turn 零 JSONL 输出（真撞限签名：API 直接拒绝、什么都不写）再静默 `rate_limit_confirm_quiet`（默认 15s）才确认；turn 正常完成时清残留 flag 防毒化下一 turn；结构化 `rate_limit_event` 仍立即可信。
- **教训**：对终端输出做文本匹配的任何"带外信号"都必须考虑**信号文本被会话自己渲染**的自指场景（本仓库的代码/测试里就含有全部标记字符串）；sticky 进程级 flag 要有显式清除路径；判定要与权威数据源（JSONL）交叉验证而非单一文本信号定罪。
- **Commit**: 3434cd3

## 2026-06-12 全新 config_dir 首次交互模式卡 theme picker

### 问题：headless 供给的 config_dir 第一次跑 PTY，claude 卡在 onboarding

- **现象**：elastic-agent worker（config_dir 由 OAuth 流程直接写 credentials 供 `-p` 用）上 PTY 会话 spawn 后进程活着但 JSONL 永远不出现——claude 停在 "Let's get started / Choose the text style" 的 theme 选择框，stdin 注入的 prompt 无人消费。
- **根因**：`-p` 模式从不显示全局 onboarding，所以纯 headless 用过的 config_dir 的 `.claude.json` 没有 `hasCompletedOnboarding`；首次交互模式（PTY）必弹 theme picker。pretrust 只写了 projects trust 条目，drain 的 "Entertoconfirm" 匹配不到这个对话框文案。
- **解决**：`_pretrust_workdir` 顺带 setdefault 顶层 `hasCompletedOnboarding: true` + `theme: "dark"`（已有用户选择不覆盖）。
- **教训**：交互模式与 headless 模式的启动路径差异要逐个对话框排查；"进程活着但 JSONL 不出现" = 卡 TUI 对话框的标志性症状，诊断手段是 `script -qec` 复现 + 剥 ANSI 看屏幕。
- **Commit**: eee68a5

## 2026-06-11 同机多宿主注入串话（task-inject-isolation）

### 问题：BridgeHub 注入打进了别人的会话

- **现象**：elastic-agent 接入冒烟测试时，新宿主进程的 channel 注入直接落进了同机另一个宿主（pty-bridge）的会话上下文，且 /inject 返回 200，注入方以为成功；目标会话靠 inject_confirm_timeout + stdin fallback 兜底才完成任务。
- **根因**（两层）：
  1. `SessionPool._next_inject_port` 是实例级计数器、固定从 19100 起——同机两个宿主进程的第一个会话必然撞端口；
  2. channel_server 的 `/inject` 不校验目标 session——打错端口的消息照单全收并回 200。
- **解决**：
  1. pool 改用 OS 分配空闲端口（bind ("127.0.0.1", 0) 取 port），消除确定性碰撞；
  2. `BridgeHub.inject` 负载携带目标 `session_id`，channel_server 不匹配回 409（无 session_id 的旧宿主请求兼容放行）——即使端口碰撞/复用，消息也不可能漏进别的会话，注入方拿到 False 走 stdin fallback；
  3. channel_server 端口 bind 失败不再崩整个 MCP server，仅禁用注入（stdin fallback 仍可用）。
- **教训**：localhost 多进程间的"端口即身份"不可靠——任何跨进程投递都要带显式收件人校验；固定起始的端口计数器在单进程内看似安全，多宿主部署立即失效。回归测试要覆盖"两个宿主同机共存"的场景。
- **Commit**: aa23aab

## 2026-06-10 Phase 1: I/O 核心重构（commits 1f889cc, 0c64014）

### 问题 1：channels 路径整体是坏的，但没有任何测试发现

- **现象**：`--dangerously-load-development-channels` 启动时会弹 "I am using this for local development / Enter to confirm" 确认对话框。旧的触发词列表（trust/safety/...）不含它 → CC 永远卡在对话框 → channel MCP server 从未启动 → inject 全部 Connection refused。
- **解决**：(a) spawn 前预写 `.claude.json` 的 `projects[cwd]`（hasTrustDialogAccepted + enabledMcpjsonServers）从根上消灭对话框；(b) drain loop 改为通用匹配——剥 ANSI + 折叠空白后找 `Entertoconfirm`（CC 用光标定位渲染，可见空格不是真空格），600ms 冷却后主动 re-check（连续第二个对话框渲染完后 CC 静默，没有新输出驱动检查）。
- **教训**：枚举具体对话框文案必然漏；要匹配对话框的结构特征（"Enter to confirm" 是所有确认框的统一尾缀）。**任何"理论上能用"的路径必须有端到端集成测试**（本次补了 TestChannelInjectionIntegration）。

### 问题 2：`stop_reason=end_turn` 不是可靠的回合结束信号

- **现象**：同一 turn 内 thinking 块和 text 块各占一条 JSONL 行，都带 `end_turn`，首条命中即截断事件流，trailing 消息丢失。
- **解决**：改用 `{"type":"system","subtype":"turn_duration"}` 哨兵——CC 在交互模式下每 turn 恰好写一条，且在所有 trailing 消息之后（spike 实测验证）。
- **教训**：对 CC 内部格式的假设必须先用真实进程 dump 验证，不要照搬 headless stream-json 的经验（交互模式没有 `result` 事件，cost 也要改从 usage 累加）。

### 问题 3：jsonl_path 推导规则错误

- **现象**：`cwd.replace("/","-").replace("_","-")` 漏掉 `.`、空格等字符；cwd 含这些字符时轮询的 JSONL 路径不存在，永远读不到事件。
- **解决**：spike 实测 CC 真实规则为 `re.sub(r"[^A-Za-z0-9]", "-", cwd)`（所有非字母数字 → `-`，大小写保留）。
- **教训**：逆向第三方路径规则时用边界字符做实验（`.` `_` 空格 `@` 大写），不要只验证 happy path。

### 问题 4：PTY stdin 逐字符输入危险且缓慢

- **现象**：多字节 UTF-8 被按"字符"拆分发送但每字符间 sleep（中文/emoji 场景脆弱）；prompt 含 `\n` 会被 TUI 当作提交；1 万字符要 8 分钟。
- **解决**：输入主路改 channel 注入（MCP notification，完全绕开 TUI 输入层）；stdin 仅作 fallback，且改为 bracketed-paste（`\x1b[200~...\x1b[201~`）整段写入——换行不再触发提交。
- **教训**：PTY 适合当"宿主"（保活/中断/启动应答），不适合当消息通道；结构化输入输出都该走协议层（MCP/JSONL）。参考 Teleos（zjw49246/Agent2Agent）的生产验证。

## 2026-06-11 生产 task 80 静默挂死复盘（commit b067662）

### 问题 1：channel 注入"假成功"——消息黑洞 30 分钟

- **现象**：用户开 PTY 发消息，日志显示 `prompt delivered via channel`（注入发生在 resume spawn "started" 后仅 13ms），但 session JSONL 永远没出现 user 事件——CC 当时仍在初始化，notification 被静默丢弃。stdin fallback 只覆盖 `inject 返回 false`，不覆盖"返回 200 但 CC 没消费"。
- **解决**：`send_prompt` 在 channel 投递后启动确认窗口（`inject_confirm_timeout`，默认 15s）：窗口内 JSONL 出现任何新消息即视为 turn 已启动；否则 stdin 重投一次（bracketed-paste）。
- **教训**：**跨进程投递的"成功"必须以接收方的 ground truth（JSONL）确认，发送方的 HTTP 200 不算数**。`/inject` 的 200 只意味着 notification 写进了 channel server 的 stdout 管道。

### 问题 2：API 错误掐断 turn，轮询层永远等不到哨兵

- **现象**：turn 进行中 API 返回 Usage Policy 拒绝，CC 写入一条 `isApiErrorMessage: true` 的 assistant 消息后 turn 终止——**不会再写 `turn_duration` 哨兵**。轮询层只认哨兵 → 静默挂到 response_timeout（30 分钟），用户侧无任何反馈。
- **解决**：与 rate-limit 检测同类处理——session 循环检测 `isApiErrorMessage`，立即 yield 错误事件结束 turn；normalize 时对应 assistant 事件标 `is_error=True`。
- **教训**：哨兵协议要枚举"哨兵不会来"的所有路径（rate-limit、API error、进程死亡），每条都要有主动终止信号，否则就是静默挂死。

## 2026-06-12 生产 task 87 回复错位复盘（commit 14ce6a0）

### 问题：后台子 agent 唤醒的自主 turn 无人消费，回复永久 +1 错位

- **现象**：模型用内置 Monitor 工具挂了后台监视器并正常结束 turn；之后 harness 用 `<task-notification>` 自主唤醒 session 跑了多个 turn——此时没有任何 consumer 在读 JSONL。用户再发消息时，新 `send_prompt` 读到积压事件，碰到**旧 turn 的 `turn_duration`** 即判"本次回答结束"，把上一个 turn 的输出当回复推给用户。从 07:14 起每条消息的回复都错一位，持续到会话结束（用户同一句话发两遍仍对不上）。
- **根因**：① turn 结束判定没有和"是哪个 prompt 的 turn"关联；② session 空闲期（自主 turn）完全无人消费 JSONL。
- **解决**：① `send_prompt` 投递前 drain 积压事件并标 `orphan`，只有看到**本次 prompt 的 user 回显**后才认 `turn_duration`（CC 总会把投递的 prompt 回写为 user 消息，channel 注入带 `<channel>` 包装、stdin 原文，子串匹配即可）；② Session 常驻空闲 watcher，turn 间持续消费自主 turn 并以 `autonomous=True` 经 on_event 上报；③ 顺带把 response_timeout 改为不活动超时（任意 JSONL 行/子 agent transcript 增长都算活动），挂起子 agent 的 session 不被池驱逐。
- **教训**：**轮询 + 哨兵的协议必须做"会话归属"校验**——哨兵只说明"某个 turn 结束了"，不说明"你的 turn 结束了"。凡是"接收方可能自己说话"的通道（harness 自主唤醒、后台任务通知），都必须有常驻消费者，否则积压必然错位到下一次读取。
