# PTY 常驻会话方案 — 完整技术说明

> 用常驻交互式 Claude Code 进程替代 `claude -p` 一次性调用的完整方案。
> 配套项目：[Claude-Code-PTY](https://github.com/zjw49246/Claude-Code-PTY)（本仓库）+ [Claude-Code-Manager](https://github.com/zjw49246/Claude-Code-Manager)（CCM，调用方）。
> 设计参考：Teleos（zjw49246/Agent2Agent）的生产验证经验。

---

## 1. 背景：为什么要替换 `claude -p`

CCM 原本通过 headless 模式调用 Claude Code：

```bash
claude -p "<prompt>" --output-format stream-json --resume <session_id> ...
```

这个模式简单可靠（进程退出 = 回合结束，exit code = 成败），但有结构性代价：

| 问题 | 说明 |
|---|---|
| **每轮冷启动** | 每条 chat 消息、每个 loop 迭代都要重新拉起整个 CC 进程，加载上下文 |
| **上下文重放** | `--resume` 每次都要从 JSONL 重建对话历史，会话越长越慢 |
| **无法中途干预** | 进程跑起来后只能 SIGINT 杀掉，不能注入新消息、不能软中断 |
| **回合间上下文不热** | KV cache、文件状态等进程内状态每轮丢失 |

**PTY 方案的目标**：一个任务对应一个常驻的交互式 CC 进程，多轮对话在同一进程内完成——第二轮起免冷启动、上下文热、支持运行中注入与软中断，同时**功能与 -p 模式 1:1 对齐**，可随时切回。

---

## 2. 核心设计哲学：PTY 只当宿主，消息走协议层

最初的直觉是"用伪终端模拟人类打字、解析终端输出"——这条路被证明是错的：

- 逐字符写 stdin：UTF-8 多字节字符被拆分、prompt 含换行会提前提交、长 prompt 极慢、TUI 状态易被污染
- 解析 ANSI 终端输出：Ink 渲染用光标定位，输出极不稳定，不可能可靠还原结构

正确的架构（借鉴 Teleos）是 **PTY 仅负责进程托管，输入输出都走结构化协议**：

```
┌──────────────────────────────────────────────────────────────┐
│                        CCM 后端                                │
│   instance_manager.launch()  ──── use_pty_mode? ──┐           │
│        │ (flag off → 原 claude -p 路径，零改动)     │           │
└────────┼──────────────────────────────────────────┼───────────┘
         ▼                                          ▼
   claude -p 子进程                          CCMBackend (claude_pty)
   (stream-json stdout)                             │
                                    ┌───────────────┼───────────────┐
                                    │        SessionPool (LRU)       │
                                    │  Session = 1 个常驻 CC 进程     │
                                    └───────────────┬───────────────┘
                  ┌─────────────────────────────────┤
                  │输入                              │输出
                  ▼                                  ▼
      BridgeHub (localhost HTTP)          JsonlReader 轮询
                  │ POST /inject          ~/.claude/projects/<hash>/<sid>.jsonl
                  ▼                                  │
      channel_server (MCP stdio)                     │ normalize()
                  │ notifications/claude/channel     ▼
                  ▼                       PTYEvent（与 CCM StreamParser
      Claude Code 交互式进程                对齐的事件结构）
      （PTY 仅做：保活/启动应答/
        Esc 中断/活动信号）
```

三条通道各司其职：

1. **输入 = Channel 注入**（主路）：MCP notification，完全绕开 TUI 输入层
2. **输出 = JSONL 轮询**：CC 自己写的会话记录文件是唯一事实来源
3. **PTY = 宿主**：进程保活、启动对话框应答、Esc 软中断、输出活动信号

---

## 3. 关键机制详解

### 3.1 进程托管（`pty_process.py`）

```python
master, slave = pty.openpty()                  # 伪终端对
fcntl.ioctl(slave, TIOCSWINSZ, 50x200)         # 固定终端尺寸
subprocess.Popen(
    ["claude",
     "--dangerously-load-development-channels", "server:pty-bridge",  # 启用 channel
     "--dangerously-skip-permissions",
     "--session-id", <uuid>],                  # 或 --resume <uuid>（恢复已有会话）
    stdin=slave, stdout=slave, stderr=slave,
    start_new_session=True, cwd=cwd, env=clean_env)
```

- 环境清理：剔除 `CLAUDE*` / `AI_AGENT*` 变量（避免嵌套会话检测），强制 `TERM=xterm-256color`、UTF-8 locale
- `CLAUDE_CONFIG_DIR` 按需注入（号池换号用）
- 守护线程 drain loop 持续排空 master_fd（防止 CC 因输出缓冲满而阻塞），同时承担启动应答与活动信号

### 3.2 启动对话框：预写 + 自动应答双保险

交互式 CC 启动会弹对话框（trust 确认、dev-channels 警告、主题选择），任何一个没应答都会卡死整个会话。两层防御：

**第一层（主）：spawn 前预写 `~/.claude.json`**

```json
"projects": {
  "<cwd>": {
    "hasTrustDialogAccepted": true,
    "hasClaudeMdExternalIncludesApproved": true,
    "hasClaudeMdExternalIncludesWarningShown": true,
    "projectOnboardingSeenCount": 1,
    "enabledMcpjsonServers": ["pty-bridge"]     // 预批 MCP server
  }
}
```

原子写入（tmp + rename），已有字段保留。trust 对话框从根上不出现。

**第二层（兜底）：drain loop 通用自动应答**

CC 的 TUI 用光标定位排版，可见的空格不是真实空格——所以匹配必须 **剥 ANSI 转义 + 折叠所有空白**，然后找统一尾缀 `"Entertoconfirm"`（所有确认框共有），命中写 `\r`：

```python
_collapse_for_prompt_match(data)   # 剥 ANSI + re.sub(r"\s+","")
if "Entertoconfirm" in buf: write(b"\r")
```

细节：600ms 冷却后**主动 re-check**（连续第二个对话框渲染完后 CC 静默，没有新输出驱动检查）；20 秒窗口后失效（避免运行期误判）。

> 教训：旧版枚举具体文案（"trust"/"safety"...）漏掉了 dev-channels 警告框，导致 channel 路径整体失效。匹配结构特征而非具体文案。

### 3.3 输入通路：Channel 注入（主）+ bracketed-paste stdin（fallback）

```
Session.send_prompt(text)
  └─ _deliver_prompt:
       ① bridge.inject(sid, text)      ← HTTP POST → channel_server
            重试 5 次 × 1s              ← MCP notification 写 CC stdin
       ② 全部失败 → PTY stdin fallback   ← \x1b[200~ + text + \x1b[201~ + \r
```

- **Channel 注入**：消息以 `<channel source="pty-bridge">` 形式进入 CC 上下文。**关键性质（spike 验证）：CC idle 时收到 channel notification 会自动开启新 turn**——这是整个方案的承重墙。绕开 TUI 输入层意味着：prompt 内容永远不会触碰快捷键/斜杠补全/粘贴处理。
- **stdin fallback**：bracketed-paste 包裹整段写入——无 UTF-8 拆分、内嵌换行不会提前提交、任意长度瞬时到达。

链路组件：

- **BridgeHub**（宿主进程内，localhost HTTP）：session_id → channel_server 端口路由；反向接收 CC 的 reply/权限请求
- **channel_server**（`claude-pty-channel`，每会话一个 MCP stdio 子进程）：CC 启动时通过 cwd 下 `.mcp.json` + `--dangerously-load-development-channels server:pty-bridge` 加载；监听 HTTP /inject，转成 JSON-RPC notification

### 3.4 输出通路：JSONL 轮询 + 事件归一化

CC 交互模式把全部会话写到：

```
~/.claude/projects/<re.sub(r'[^A-Za-z0-9]', '-', cwd)>/<session_id>.jsonl
```

> 路径规则是 spike 实测的：**所有非字母数字字符 → `-`，大小写保留**（旧实现漏了 `.`/空格，cwd 含这些字符时整个输出通路失明）。

`JsonlReader`：300ms 增量轮询，行缓冲处理 partial write（不完整尾行留到下次拼接，offset 按 UTF-8 字节推进），然后 `normalize()` 成与 CCM `StreamParser` 完全对齐的事件：

| JSONL 行 | PTYEvent | 说明 |
|---|---|---|
| `assistant` + text block | `message` | 正文 |
| `assistant` + thinking block | `thinking` | 思考（每条 assistant 带 usage → context_usage） |
| `assistant` + tool_use block | `tool_use` | 工具调用 |
| `user` + tool_result block | `tool_result` | 工具输出 |
| `system/turn_duration` | （回合结束信号） | 见 3.5 |
| `mode` / `permission-mode` / `file-history-snapshot` / `attachment` / `ai-title` / `queue-operation` | 跳过 | 噪声 |

### 3.5 回合结束检测：`turn_duration` 哨兵

**交互模式没有 `result` 事件**（那是 -p stream-json 专属）。可靠的回合结束信号是：

```json
{"type": "system", "subtype": "turn_duration", "durationMs": 3348}
```

spike 实测：每 turn 恰好一条、出现在所有 trailing 消息之后。

> 为什么不用 `stop_reason == "end_turn"`：同一 turn 的 thinking 块和 text 块各占一条 JSONL 行、**都带 end_turn**，按它判断会提前截断事件流丢失尾部消息。

三层兜底：`turn_duration` 哨兵（确定性）→ `response_timeout`（默认 30 分钟，与 CCM 任务超时对齐）→ dispatcher 超时 kill。

### 3.6 会话生命周期

- **SessionPool**：LRU 管理（默认上限 20），驱逐优先 idle 超时的会话，mid-turn（send_lock 占用）永不驱逐
- **热复用**：同 session_id 再次 launch → 池中活会话直接注入新 prompt（免冷启动，实测第二轮 7.8s 完成）
- **冷恢复**（`resume_existing`）：会话不在池中但磁盘有记录 → `--resume <sid>` 拉起（注意不是 `--session-id`，那会与已有会话冲突）
- **崩溃自愈**：进程死亡 → 指数退避 `--resume` 重启（上限 3 次）
- **换号迁移**（`migrate_session`）：JSONL 硬链接到新 `CLAUDE_CONFIG_DIR` → 用新账号 `--resume`，上下文不丢
- **drain_idle**：宿主关闭 PTY 模式时回收全部 idle 会话，mid-turn 跑完为止

---

## 4. 与 CCM 的配套使用

### 4.1 接入点：一个 flag、一处分流

CCM 侧改动刻意最小化。`InstanceManager.launch()` 开头分流：

```python
if provider == "claude" and self.pty_mode_enabled:
    return await self._launch_pty(...)      # → CCMBackend.launch_for_ccm
# 否则走原 claude -p 路径（代码原样保留）
```

`CCMBackend`（claude_pty 提供的适配器）负责把 PTY 世界映射回 CCM 期望的接口：

| CCM 期望 | PTY 模式的实现 |
|---|---|
| `process.wait()` / `returncode` | `_PTYProcessProxy`：回合结束（turn_duration）→ `complete(0)` 解除 wait |
| 进程退出 = 回合结束 | 消费者收完一个 turn 的事件即"退出"，**底层进程保活留给下轮** |
| exit code 语义 | turn 正常完成 → 0；超时被 dispatcher kill → proxy.kill() 真正回收会话并返回 -9 |
| stream-json 事件 → `_process_event` | `PTYEvent.to_dict()` 结构与 StreamParser 输出一致，直接复用 DB 写入 + WebSocket 广播 |
| SIGINT 停止 | `stop()` 分流：PTY 实例走 Esc 软中断 + 会话回收 |
| `--resume <session_id>` | 池中活会话热复用；不在池中冷恢复 |
| `--mcp-config`（$ skills） | 透传给 PTYConfig，CC 启动时一并加载 |
| `CLAUDE_CONFIG_DIR`（号池） | PTYConfig.config_dir 注入 |

### 4.2 任务类型映射

| CCM 场景 | PTY 模式行为 |
|---|---|
| 主任务执行 | 新建常驻会话，channel 注入 prompt |
| **chat 多轮**（收益最大） | 同一活会话直接注入，第二轮起免冷启动 |
| loop / goal 轮次 | 同一会话注入下一轮 prompt |
| goal 评估器 | **保留 -p**（`--max-turns 1` 一次性评估本来就该用 -p） |
| monitor 子 agent | **保留 -p**（独立机制，互不影响） |
| codex provider | 不受影响 |

### 4.3 前端开关（运行时切换）

- 导航栏右侧 `PTY` toggle（绿=开/灰=关），调 `PUT /api/settings/runtime`
- **粒度 = 每次 launch**：切换立即影响下一次启动（每条消息/每轮迭代都是一次 launch）
- **运行中的 turn 永不被切换**：跑完为止，事件照常入库
- 关闭时自动 `drain_idle`：idle PTY 进程立即回收
- 状态持久化到 `GlobalSettings` 表，后端重启自动恢复；env `USE_PTY_MODE` 仅为初始缺省
- 上下文无缝衔接：PTY 和 -p 共享 session_id/JSONL，切换后 `--resume` 接上历史

### 4.4 部署要求

```bash
# CCM venv 内安装（dev 环境已就绪）
pip install -e /path/to/Claude-Code-PTY    # 提供 claude_pty 包 + claude-pty-channel 入口

# 启动 CCM（开关初始值可用 env 控制，之后前端可随时切）
USE_PTY_MODE=true AUTH_TOKEN=... uvicorn backend.main:app --port 8003
```

`claude-pty-channel` 必须在 CC 子进程的 PATH 中（venv 内启动后端即满足）。

---

## 5. 验证体系

| 层级 | 内容 |
|---|---|
| Phase 0 spikes | ① channel 注入唤起 idle 会话新 turn ✅ ② 交互 JSONL 形态（无 result、turn_duration 哨兵）③ 路径规则实测 ④ trust 预写有效 ✅ |
| PTY 单测（96） | 路径规则、哨兵判定、预写、Entertoconfirm 匹配、注入 fallback、resume、drain_idle… |
| PTY 集成（6，真实 CC） | 含 channels 端到端：send_prompt → 注入 → tool_use → turn_duration |
| PTY 全功能（7，真实 CC） | 多轮记忆、事件格式 CCM 兼容性等 |
| CCM 单测（67） | launch 分流、stop 分流、运行时开关、API 往返、drain 触发 |
| 端到端冒烟（`scripts/pty_smoke.py`） | 真实 InstanceManager + CCMBackend + CC：事件入库/广播/exit 0/**热复用 7.8s** |

---

## 6. 已知边界与路线图（Phase 3）

| 项 | 现状 | 计划 |
|---|---|---|
| **号池撞限自动换号**（最高优先） | 基础设施就绪但**检测缺失**：交互模式撞限不退进程，表现为超时而非 rotation | 扫 PTY 输出 usage-limit 标志（Teleos stderr-ring 玩法）→ 调 `migrate_and_relaunch()` |
| cost 统计 | 无 result 事件，`total_cost_usd` 不更新 | 从各 assistant 消息 usage 累加 |
| 任务级完成信号 | 依赖 turn_duration（回合级） | `ccm_done(summary)` MCP tool（学 teleos_done） |
| 崩溃分类 | 统一 `--resume` 重试 | 区分 auth 过期/binary 缺失（不重试）vs 运行期崩溃；stderr 环形缓冲诊断 |
| typing/idle 信号 | drain loop 已记录 `_last_output` | 暴露给 CCM 做前端 typing indicator |
| `.mcp.json` 写入用户 cwd | 有污染风险 | 改临时目录 + `--mcp-config`，或 stop 时清理 |

---

## 7. 代码索引

**Claude-Code-PTY**（`src/claude_pty/`）

| 文件 | 职责 |
|---|---|
| `pty_process.py` | PTY 托管、spawn、预写 trust、drain loop 自动应答、bracketed-paste stdin |
| `session.py` | 高层会话：`send_prompt`（channel 优先/stdin fallback）、auto-resume、migrate |
| `jsonl_reader.py` | JSONL 增量读取、normalize、turn_duration 判定 |
| `pool.py` | SessionPool：LRU、驱逐、drain_idle |
| `bridge.py` | BridgeHub：注入路由、reply/权限回调 |
| `channel_server.py` | claude-pty-channel：MCP stdio server，HTTP /inject → notification |
| `adapters/ccm.py` | CCMBackend + `_PTYProcessProxy`：CCM 接口映射 |

**Claude-Code-Manager**（dev 分支）

| 文件 | 改动 |
|---|---|
| `backend/config.py` | `use_pty_mode` 设置 |
| `backend/services/instance_manager.py` | launch/stop 分流、`set_pty_mode`、`drain_idle_pty_sessions` |
| `backend/api/settings.py` | `GET/PUT /api/settings/runtime` |
| `backend/models/global_settings.py` | `use_pty_mode` 列（migration `b7c8d9e0f1a2`） |
| `frontend/.../Layout/Header.tsx` | 导航栏 PTY toggle |
| `scripts/pty_smoke.py` | 端到端冒烟 |

**关键 commits**：PTY `1f889cc`（I/O 核心）→ `0c64014`（channel 输入）→ `a02289e`（CCM 适配）→ `0b59e90`（drain_idle）；CCM-dev `1b6d45b`（接线）→ `55374fe`（前端开关）→ `e7b23ec`（drain）。

---

*文档版本：2026-06-10，对应 Claude Code 2.1.168。*
