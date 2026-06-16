# PTY 框架技术方案总结

## 1. 核心思路

PTY 框架的本质是：**用一个持久的交互式 Claude Code 进程替代每次调用都要冷启动的 `-p` 模式**。

Claude Code 有两种使用方式：

- **`-p` 模式（headless）**：每条消息 = 启动进程 → 从磁盘 JSONL 重放历史 → 重建 KV cache → 处理 → 退出（cache 丢弃）
- **PTY 模式**：启动一次，常驻内存，后续消息通过 MCP channel 注入，KV cache 热复用

**设计哲学**：PTY 只当宿主（进程保活、启动应答、Esc 中断），消息走协议层（MCP channel 输入、JSONL 输出），绝不做终端文本解析。

## 2. 架构概览

### 2.1 总体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│ 你的后端进程                                                      │
│                                                                   │
│  SessionPool ─────┬─────→ Session A (warm, 热复用)               │
│   (LRU, max 20)  ├─────→ Session B (idle, 待回收)               │
│                   └─────→ Session C (mid-turn, 执行中)           │
│                   │                                              │
│                   └─→ BridgeHub (HTTP server, localhost:自动分配) │
│                        ├─→ Session→Port 映射表                   │
│                        ├─→ /inject 路由                          │
│                        └─→ /permission_request 路由              │
└────────────────────────┼─────────────────────────────────────────┘
                         │ HTTP POST /inject
                         ▼
    ┌──────────────────────────────────────┐
    │ Session 的 PTYProcess                │
    │                                      │
    │  PTY master_fd ──┐                   │
    │  ├─→ drain_loop  │ (守护线程)        │
    │  │   · 自动应答启动对话框             │
    │  │   · 扫描限额横幅                  │
    │  │   · 更新 _last_output 时间戳      │
    │  │                                   │
    │  ├─→ stdin 写入 (bracketed-paste)    │
    │  │                                   │
    │  └─→ JsonlReader                     │
    │      轮询 ~/.claude/projects/        │
    │        <cwd-hash>/<session_id>.jsonl  │
    │      → normalize → PTYEvent          │
    └──────────────┬───────────────────────┘
                   │ master_fd ↔ slave_fd
                   ▼
    ┌──────────────────────────────────────┐
    │ claude (交互模式, 运行在 PTY 中)      │
    │                                      │
    │ · 渲染 TUI 界面                      │
    │ · 处理 prompt                        │
    │ · 写 JSONL 会话记录                  │
    │ · 运行 MCP servers:                  │
    │   └─ pty-bridge (channel_server)     │
    │      · stdin: JSON-RPC 协议          │
    │      · HTTP /inject ← BridgeHub     │
    │      · → notifications/claude/channel│
    └──────────────────────────────────────┘
```

### 2.2 数据流路径

```
输入路径（prompt → Claude Code）:
    Session.send_prompt("请帮我...")
      │
      ├─[主路径] BridgeHub.inject()
      │   → HTTP POST http://127.0.0.1:<inject_port>/inject
      │   → channel_server 验证 session_id（不匹配返回 409）
      │   → MCP notification: {"method": "notifications/claude/channel", "params": {"content": "..."}}
      │   → Claude Code 收到: <channel source="pty-bridge">请帮我...</channel>
      │   → 唤醒 idle 会话，开始新 turn
      │
      └─[回退] PTY stdin (bracketed-paste)
          → \x1b[200~ + 消息 + \x1b[201~ + \r
          → 原子写入，避免 UTF-8 拆分和换行符提前提交

输出路径（Claude Code → 事件流）:
    Claude Code 写入 JSONL
      → ~/.claude/projects/<re.sub(r'[^A-Za-z0-9]','-',cwd)>/<session_id>.jsonl
      → JsonlReader 每 300ms 轮询，增量读取（UTF-8 字节偏移 + 行缓冲）
      → normalize() 映射为 PTYEvent（兼容 CCM StreamParser）
      → async for event in session.send_prompt(): yield event
```

## 3. PTY vs `-p` 全面对比

### 3.1 核心差异表

| 维度 | `-p` 模式 | PTY 模式 |
|---|---|---|
| **进程生命周期** | 每条消息启动/退出一次 | 启动一次，常驻内存 |
| **KV cache** | 每轮丢弃，下轮从 JSONL 重建 | 跨轮保留，增量更新 |
| **第二轮延迟** | 30-60 秒（随对话变长线性增长） | ~7-8 秒（实测） |
| **多轮成本** | O(n²)：第 k 轮重放前 k-1 轮所有内容 | O(n)：每轮只付增量 |
| **中断能力** | SIGINT 杀进程（会话丢失） | Esc 软中断（会话保留） |
| **工具状态** | 每轮丢失，需重新初始化 | 跨轮保留 |
| **会话恢复** | 需要 `--resume` + 完整重放 | 进程存活则零开销复用 |
| **并发会话** | 每个会话独立进程 | SessionPool LRU 管理 |
| **输入方式** | stdin pipe（一次性） | MCP channel 注入（结构化） |
| **输出方式** | stdout JSON stream | JSONL 文件轮询 |

### 3.2 延迟对比详解

**`-p` 模式每轮做的事（从发送到收到首 token）**：

1. `fork + exec claude` — 进程启动 ~2-3s
2. 加载配置、初始化 SDK — ~1-2s
3. 从磁盘读取 JSONL 历史 — 随历史增长
4. 将历史序列化为 API 请求 — 随历史增长
5. API 请求发送，等待 KV cache 计算 — 随上下文长度增长
6. 首个 token 返回

**PTY 模式每轮做的事**：

1. BridgeHub HTTP POST → channel_server — ~50ms
2. MCP notification 写入 CC stdin — ~10ms
3. CC 从 idle 唤醒，组装增量请求 — ~100ms
4. API 请求发送，**增量** KV cache 计算 — 只计算新 token
5. 首个 token 返回

**关键差异**：步骤 3-5 在 PTY 模式下是增量的，不随对话历史线性增长。

## 4. 重点：省钱分析

### 4.1 理解 Claude API 计费模型

Claude API 的计费基于 **input tokens**（发送给模型的）和 **output tokens**（模型生成的）。其中 input tokens 有一个重要优化：

- **Prompt Caching**（服务端）：如果请求的前缀与最近一次请求相同，缓存命中部分按 **1/10 价格** 计费
- **缓存 TTL**：5 分钟。超过 5 分钟未复用，缓存失效，下次全价

### 4.2 `-p` 模式的 token 浪费

每轮对话，`-p` 模式必须发送**完整历史**到 API：

```
第 1 轮: [system prompt + 用户消息 1]                    = S + M₁ tokens
第 2 轮: [system prompt + 消息 1 + 回复 1 + 消息 2]      = S + M₁ + R₁ + M₂ tokens
第 3 轮: [system prompt + 消息 1-2 + 回复 1-2 + 消息 3]  = S + M₁ + R₁ + M₂ + R₂ + M₃ tokens
...
第 n 轮: S + Σ(Mᵢ + Rᵢ) for i=1..n-1 + Mₙ tokens
```

**总 input token 消耗** ≈ O(n²)（假设每轮平均内容量相近）

虽然服务端 prompt caching 能缓解（命中时 1/10 价格），但有两个关键限制：

1. **5 分钟 TTL**：coding agent 一轮可能执行 3-5 分钟（读代码、写代码、运行测试），下一轮时 cache 可能已过期
2. **冷启动开销**：每次 `-p` 启动都是新进程，没有任何进程级 cache 可复用
3. **cache miss 代价高**：200K context window 的 cache miss = 全价计算所有 token 的 KV

### 4.3 PTY 模式如何省钱

PTY 模式的省钱来自 **两层 cache 复用**：

**第一层：进程级 KV cache 热复用**

Claude Code 进程不退出，本地 KV cache 常驻内存。Claude Code 内部对 API 的调用可以利用这一点：
- 历史部分 token 的 KV 值已经计算过，不需要重新计算
- 只需要处理新增的增量内容
- 这是**进程级别**的优化，与服务端 cache 独立

**第二层：服务端 prompt cache 命中率提升**

PTY 模式下，连续对话间隔通常 < 5 分钟（人类交互节奏），服务端 cache 大概率命中：
- 命中部分按 **1/10 价格** 计费
- `-p` 模式因为冷启动 + 进程重建，间隔更长，cache 命中率更低

**第三层：间接省钱**

- **工具状态保留**：不需要重新读文件、重新理解代码结构，减少工具调用的 token 开销
- **崩溃恢复**：自动 `--resume` 而非从头开始新会话
- **会话迁移**：hardlink JSONL 到新账户目录，保留全部历史，不需要重建

### 4.4 量化估算

假设一个典型 20 轮编程任务，每轮平均上下文增长 5K tokens（system prompt 10K）：

**`-p` 模式 input token 消耗**：

| 轮次 | 发送 tokens | 累计消耗 |
|------|------------|----------|
| 1 | 10K + 5K = 15K | 15K |
| 2 | 15K + 5K + 5K = 25K | 40K |
| 5 | 55K | 205K |
| 10 | 105K | 655K |
| 20 | 205K | 2,255K |

**总 input**: ~2.26M tokens

假设 prompt cache 命中率：
- 乐观场景（每轮 < 5 分钟）：~60% 命中 → 实际成本 ≈ 2.26M × (0.4 × 全价 + 0.6 × 1/10 价)
- 悲观场景（部分轮次 > 5 分钟）：~30% 命中 → 成本显著增加

**PTY 模式 input token 消耗**：

进程常驻，KV cache 热复用，每轮只需发送**增量**：
- 真正的"新 input"只有每轮新增的 ~10K tokens（用户消息 + 工具结果）
- 历史部分通过 KV cache 复用，无需重新计算

| 轮次 | 新增 tokens | 累计新增 |
|------|------------|----------|
| 1 | 15K（首轮全量） | 15K |
| 2-20 | ~10K × 19 | 190K |

**总 input**: ~205K tokens（首轮全量 + 后续增量）

加上 prompt caching（进程不退出，间隔短，命中率 > 80%）：
- 历史部分大部分 cache 命中（1/10 价）
- 实际全价 token ≈ 新增部分

### 4.5 成本对比表（以 Claude Sonnet 为例）

| 指标 | `-p` 模式 | PTY 模式 | 节省比例 |
|------|----------|----------|---------|
| 总 input tokens（20 轮） | ~2.26M | ~205K（增量） | ~90% |
| Cache 命中率 | 30-60% | 80%+ | - |
| 等效全价 tokens | ~1.2M-1.6M | ~200K-400K | **70-85%** |
| 第二轮延迟 | 30-60s | 7-8s | **80-85%** |

**结论：对话越长、越密集，PTY 省钱效果越显著。20 轮对话可节省 70-85% 的 input token 成本。**

### 4.6 什么场景下 PTY 优势最大

| 场景 | 省钱效果 | 原因 |
|------|---------|------|
| 多轮编程任务（10-50 轮） | **极高** | 避免 O(n²) 重放，KV cache 持续复用 |
| 快速迭代（每轮 < 2 分钟） | **极高** | 服务端 cache 几乎 100% 命中 |
| 长上下文会话（100K+ tokens） | **极高** | 重放 100K+ tokens 的 KV 计算成本巨大 |
| 单轮简单任务 | **无** | 无复用可言，PTY 启动开销甚至略高 |
| 低频对话（间隔 > 30 分钟） | **中等** | 进程复用有效，但服务端 cache 可能过期 |

## 5. 核心组件详解

### 5.1 Session — 会话管理核心

`session.py` 是整个框架的主 API 入口，提供异步会话管理。

**关键方法**：

| 方法 | 功能 |
|------|------|
| `start()` | 启动 PTY 进程，等待初始化，注册到 BridgeHub |
| `send_prompt(text)` | 发送 prompt，返回异步事件流 |
| `send_interrupt()` | 发送 Esc 软中断 |
| `stop()` | 优雅关闭会话 |
| `migrate_session(new_config_dir)` | 账户迁移（hardlink JSONL） |

**`send_prompt` 完整流程**：

```
1. 排空积压（orphan events）
   └─ 读取上一轮/自主轮遗留的 JSONL 事件
   └─ 标记 orphan=True，yield 给调用方（不混入当前回复）

2. 投递 prompt
   ├─ 主路径：BridgeHub.inject()（最多重试 5 次，间隔 1s）
   └─ 回退：PTY stdin bracketed-paste

3. 确认投递（仅 channel 路径）
   └─ 15s 内无 JSONL 活动 → stdin 重投一次
   └─ 防止"HTTP 200 但 CC 没消费"的静默丢弃

4. 轮询响应（主循环）
   ├─ 每 300ms 读取 JSONL 新行
   ├─ 检测 prompt echo（自己消息的回显）→ 标记 turn_started
   ├─ normalize → yield PTYEvent
   ├─ 检测 turn_duration sentinel → 标记 response_complete
   ├─ 检测 isApiErrorMessage → 立即终止（不会有 sentinel）
   ├─ 检测 rate_limit_event → 报错终止
   ├─ 每 5s 检查 sub-agent transcript 增长（作为活动信号）
   └─ response_timeout（7200s）基于**不活动时间**，每次 JSONL 活动都刷新

5. 收尾
   └─ post_response_wait（3s）等待尾部输出
   └─ 清除残留的限额标记（防毒化下一轮）
```

**自动恢复机制**：

```
发现进程死亡 → _auto_resume()
  └─ _restart_count < 3?
      ├─ 是：指数退避等待 2^count 秒，然后 start(resume_session_id=当前ID)
      └─ 否：raise SessionError("max restart attempts exceeded")
```

**Idle Watcher（空闲监听）**：

常驻后台任务，每 1s 检查一次：
- 如果 `send_prompt` 正在执行（持有 `_send_lock`）→ 跳过
- 否则：读取 JSONL 新事件，标记 `autonomous=True`，触发 `on_autonomous_event` 回调
- **作用**：防止 Monitor 等后台 agent 唤醒会话时事件积压，导致下一轮回复错位

### 5.2 PTYProcess — 底层 PTY 管理

**spawn() 完整流程**：

```python
① _pretrust_workdir()       # 预写 ~/.claude.json，跳过所有启动对话框
② _setup_mcp_config()       # 写 .mcp.json，配置 channel_server
③ pty.openpty()             # 创建 PTY 对（master_fd, slave_fd）
④ ioctl TIOCSWINSZ          # 设置终端大小 50行 × 200列
⑤ build_clean_env()         # 清理环境变量
⑥ subprocess.Popen(         # 启动 claude 进程
     cmd, stdin=slave, stdout=slave, stderr=slave,
     start_new_session=True  # 新进程组（Esc 中断需要）
   )
⑦ _drain_thread.start()     # 启动守护线程
```

**构建的 claude 命令**：

```bash
claude \
  --dangerously-load-development-channels server:pty-bridge \  # 启用 MCP channel
  --dangerously-skip-permissions \                              # 跳过工具权限弹窗
  --session-id <uuid>          # 新会话 (或 --resume <id> 恢复旧会话)
  [--model <model>]            # 可选：模型覆盖
  [--effort <effort>]          # 可选：effort 覆盖
  [--disallowedTools <list>]   # 可选：工具黑名单
  [--mcp-config <path>]        # 可选：额外 MCP 配置
```

**drain_loop（守护线程）详解**：

```
循环:
  select(master_fd, timeout=50ms)
  
  有数据:
    读取 ≤ 65536 字节
    更新 _last_output 时间戳
    
    ANSI 剥离 + 空白折叠 → confirm_buf（最近 2000 字符）
    匹配 "Entertoconfirm"?
      → 是：发送 \r，600ms 冷却后重检
      → 否：继续
    
    小写化 → rl_buf（最近 3000 字符）
    匹配限额关键词?（"hityoursessionlimit" / "usagelimitreached" 等）
      → 是：设置 self.rate_limited = True（sticky）
      → 否：继续
  
  无数据 + os.read 返回空:
    设置 _child_dead 事件
    触发 _on_death 回调
    退出循环
```

**预信任配置（_pretrust_workdir）写入内容**：

```json
{
  "hasCompletedOnboarding": true,     // 跳过 theme picker
  "theme": "dark",
  "projects": {
    "/absolute/path/to/cwd": {
      "hasTrustDialogAccepted": true,
      "hasClaudeMdExternalIncludesApproved": true,
      "hasClaudeMdExternalIncludesWarningShown": true,
      "projectOnboardingSeenCount": 1,
      "enabledMcpjsonServers": ["pty-bridge"]
    }
  }
}
```

原子写入：先写 `<path>.pty-tmp`，再 `os.rename()`。保留已有配置项。

### 5.3 JsonlReader — JSONL 事件归一化

**读取机制**：

- 按 UTF-8 字节偏移增量读取（不是字符偏移）
- 行缓冲处理不完整的写入（CC 可能写到一半）
- 每次返回完整 JSON 行的列表

**JSONL 记录类型映射**：

| JSONL `type` | `subtype`/条件 | PTYEvent | 说明 |
|---|---|---|---|
| `system` | `init` | `SYSTEM_INIT` | 会话启动标记 |
| `system` | `turn_duration` | **哨兵，不发事件** | 回合结束标记（唯一可靠） |
| `system` | 其他 | `SYSTEM_EVENT` | 跳过 `thinking_tokens`、`token_usage`、`api_request`、`api_response` |
| `rate_limit_event` | — | `SYSTEM_EVENT` + `is_error=True` | 结构化限额信号（立即可信） |
| `result` | — | `RESULT` | 包含 `cost_usd`、`context_usage` |
| `assistant` | text block | `MESSAGE` role=assistant | 文本回复 |
| `assistant` | thinking block | `THINKING` | 思考过程（加密的显示占位符） |
| `assistant` | tool_use block | `TOOL_USE` | 包含 `tool_name`、`tool_input` |
| `user` | text block | 通常跳过 | 仅 `include_user_text=True` 时发出（自主轮） |
| `user` | tool_result block | `TOOL_RESULT` | 工具执行结果 |
| 忽略 | — | — | `queue-operation`、`attachment`、`ai-title`、`last-prompt`、`mode`、`permission-mode`、`file-history-snapshot` |

**Prompt Echo 检测**：

```python
# 子串匹配（非精确匹配），因为：
# - Channel 注入: <channel source="pty-bridge">原始消息</channel>
# - Stdin 注入: 原始消息（无包裹）
def is_prompt_echo(raw, prompt):
    if raw["type"] != "user": return False
    texts = extract_text_blocks(raw["message"])
    needle = prompt.strip()
    return any(needle in t for t in texts)
```

这标记了**当前 turn 的真正开始**——echo 之前的事件都是上一轮的残留。

### 5.4 Channel Server — MCP 协议层

**角色**：作为 MCP server 被 Claude Code 启动，提供 channel 注入能力。

**协议**：JSON-RPC 2.0，声明能力但**不声明 tools**（防止模型通过"回复 tool"发消息，必须走正常对话）。

```json
{
  "capabilities": {
    "tools": {},
    "experimental": {
      "claude/channel": {},
      "claude/channel/permission": {}
    }
  },
  "instructions": "Messages arriving as <channel source=\"pty-bridge\"> tags are messages from the user..."
}
```

**HTTP 端点**：

| 端点 | 方法 | 功能 | 关键细节 |
|------|------|------|---------|
| `/inject` | POST | 接收消息，转为 MCP notification | 验证 session_id（不匹配返回 409） |
| `/permission_resolve` | POST | 接收权限决策 | 通过 threading.Event 通知等待线程 |
| `/health` | GET | 健康检查 | 返回 `{ok: true}` |

**注入隔离机制**：

```python
# 防止同机多宿主串话
if payload["session_id"] != self._session_id:
    return 409  # 拒绝投递给错误会话的消息
```

端口由 OS 分配（`bind("127.0.0.1", 0)`），非固定计数器，避免多实例端口冲突。

### 5.5 BridgeHub — HTTP 路由中枢

**职责**：在宿主进程中运行单一 HTTP server，路由 inject/permission 请求到正确的 channel_server。

```
BridgeHub (127.0.0.1:自动端口)
  ├─ session_ports: {session_id → inject_port}  映射表
  ├─ inject(session_id, content, meta)
  │   → urllib POST http://127.0.0.1:<inject_port>/inject, timeout=5s
  │   → 返回 bool（成功/失败）
  └─ resolve_permission(session_id, request_id, behavior)
      → urllib POST http://127.0.0.1:<inject_port>/permission_resolve, timeout=10s
```

### 5.6 SessionPool — 会话池管理

**LRU 两级淘汰策略**：

```
第一级：空闲超时淘汰
  └─ idle_seconds ≥ 300s && 无 pending sub-agents → 候选
  └─ 按最后访问时间排序，淘汰最老的

第二级：强制淘汰（仅在第一级无候选时触发）
  └─ 非 mid-prompt（_send_lock 未持有）&& 无 pending sub-agents → 候选
  └─ 按最后访问时间排序，淘汰最老的

绝不淘汰：
  └─ 有 pending sub-agents 的会话（Monitor 后台可能随时唤醒）
```

**热复用条件检查**：

```python
if session_id in pool:
    session = pool[session_id]
    config_match = (
        config_dir 相同
        AND default_model 相同
        AND default_effort 相同
    )
    if session.is_alive and config_match:
        return session  # 直接复用，零开销
    if session.is_alive and not config_match:
        session.stop()  # 配置变了，需要重建
    # 创建新 session
```

### 5.7 SubagentTracker — 原生子 Agent 追踪

**追踪的工具类型**：

| 工具名 | 类型 | 行为 |
|--------|------|------|
| `Agent` / `Task` | 同步 agent | turn 阻塞等待结果；tool_result = 完成 |
| `Monitor` | 后台 monitor | turn 结束；harness 后续通知 = 进度/完成 |

**事件映射**：

```
tool_use(Agent/Task/Monitor) → SUBAGENT_SPAWN
tool_result(Agent/Task)       → SUBAGENT_DONE
tool_result(Monitor)          → （保持 pending，等 harness 通知）
<task-notification> user msg  → SUBAGENT_PROGRESS / SUBAGENT_DONE
```

**Transcript 增长检测**：

```python
# 作为 sub-agent 活动信号
# 主 JSONL 可能静默（等待 sync agent），但 sub-agent 的 transcript 在增长
# → 说明会话仍然活跃，不应超时
def transcripts_grew():
    for fn in listdir(subagents_dir):
        if fn.endswith(".jsonl") and getsize(fn) changed:
            return True
    return False
```

## 6. 撞限检测——三重校验机制

这是最复杂的防误判机制，因为生产环境中多次出现假阳性。

### 6.1 问题背景

PTY 终端横幅（如 "You've hit your session limit"）出现在**渲染的对话内容**中也能被扫描到——例如：
- 用户/模型讨论限额话题
- tool_result 引用本仓库源码（包含限额关键词）

**真实事故**：3 个健康账户因源码引用被误判为撞限，全部冻结。

### 6.2 检测流程

```
信号 1: JSONL rate_limit_event
  └─ 完全可信，立即触发
  └─ CC 内部检测到 API 限额，写入结构化事件

信号 2: PTY 横幅扫描（drain_loop）
  └─ 单独不可信！必须交叉验证：
  
  if turn 有 JSONL 消息输出:
      → 判定假阳性（渲染内容包含限额文本）
      → 清除 rate_limited 标记，继续正常处理
  
  elif turn 零 JSONL 输出:
      → 等待 rate_limit_confirm_quiet（15s）
      → 15s 内仍无 JSONL 活动 → 确认真限额
      → 15s 内出现 JSONL 活动 → 假阳性，继续

  turn 正常完成时:
      → 清除残留 rate_limited 标记
      → 防止毒化下一轮
```

### 6.3 为什么需要这么复杂

| 场景 | 横幅 | JSONL rate_limit | JSONL 消息 | 真实情况 |
|------|------|-----------------|-----------|---------|
| 真限额（API 拒绝） | ✓ | ✓ | ✗ | 真的撞限了 |
| 真限额（仅横幅） | ✓ | ✗ | ✗ + 15s 静默 | 真的撞限了 |
| 源码引用触发 | ✓ | ✗ | ✓（正常输出） | **假阳性** |
| 讨论限额话题 | ✓ | ✗ | ✓（正常输出） | **假阳性** |

## 7. 注入确认机制——防静默丢弃

### 7.1 问题背景

Channel inject 返回 HTTP 200 只代表消息到达了 channel_server 的缓冲区，**不代表 Claude Code 真的消费了它**。

**真实事故**：CC 还在初始化时 inject 消息，HTTP 200 返回，但消息被静默丢弃，会话黑洞 30 分钟。

### 7.2 确认流程

```
send_prompt("hello")
  → BridgeHub.inject() 返回 True
  → 启动确认计时器: deadline = now + 15s

确认窗口 (15s):
  每 300ms 检查 JSONL:
    有新行? → turn_started = True, 确认成功, 退出窗口
    无新行? → 继续等待

超时 (15s 无 JSONL 活动):
  → 日志警告: "channel inject not confirmed, falling back to stdin"
  → PTYProcess.send_prompt("hello")  # 通过 bracketed-paste 重投
  → 重新开始确认等待
```

## 8. 生产事故复盘

### 8.1 限额假阳性冻结 3 账户（Task 81/82）

- **现象**：3 个健康账户被标记为 rate_limited
- **根因**：drain_loop 扫描 PTY 输出匹配到 "usagelimitreached"，实际是 tool_result 引用本仓库源码
- **修复**：JSONL 交叉验证 + 15s 静默确认 + turn 结束清除标记
- **教训**：终端文本匹配在自引用场景下必然失败，需结构化信号作为 ground truth

### 8.2 首次运行挂起在 Theme Picker（Task 81/82）

- **现象**：新 config_dir 启动 PTY 后无响应
- **根因**：`-p` 模式不弹 onboarding，所以 headless 配置的目录缺少 `hasCompletedOnboarding`；交互模式弹 theme picker，drain_loop 的 "Enter to confirm" 匹配不到
- **修复**：预写 `hasCompletedOnboarding=true` + `theme="dark"`
- **教训**：必须用 fresh config_dir 测试所有启动路径

### 8.3 多宿主注入串话（Task 86）

- **现象**：一个会话收到另一个会话的消息
- **根因**：(1) 端口用固定计数器，两个 pool 实例分配到相同端口 (2) channel_server 不校验 session_id
- **修复**：OS 分配随机端口 + inject 请求带 session_id + 不匹配返回 409
- **教训**：跨进程投递必须有显式接收方验证

### 8.4 Channel Inject 静默丢弃（Task 80）

- **现象**：消息发送成功（HTTP 200）但 CC 无反应，30 分钟后超时
- **根因**：CC 还在启动，channel_server 收到通知但 CC 没消费
- **修复**：15s inject_confirm_timeout + stdin 回退
- **教训**：发送方确认 ≠ 接收方确认，必须用接收方的 ground truth（JSONL）做最终确认

### 8.5 API Error 导致 Turn 永不结束（Task 80）

- **现象**：Usage Policy 拒绝后会话挂起
- **根因**：`isApiErrorMessage=true` 的 assistant 消息后不会有 `turn_duration` 哨兵
- **修复**：检测 `isApiErrorMessage`，立即以错误事件结束 turn
- **教训**：必须枚举所有"哨兵永不到来"的路径

### 8.6 自主轮积压导致回复错位（Task 87）

- **现象**：Monitor 后台唤醒会话 → 无人消费 JSONL → 下一轮误读旧 turn_duration 为当前回复
- **根因**：(1) turn_duration 不关联具体 prompt (2) 无 idle watcher 消费自主轮事件
- **修复**：(1) drain backlog + orphan 标记 (2) 等待自己 prompt 的 echo 才开始计数 (3) 常驻 idle watcher
- **教训**：轮询 + 哨兵模式必须包含"这是谁的 turn"关联

## 9. 配置参数全表

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `claude_binary` | `"claude"` | CC 可执行文件路径 |
| `dangerously_skip_permissions` | `True` | 自动允许工具权限 |
| `default_model` | `None` | 模型覆盖 |
| `default_effort` | `None` | Effort 覆盖 |
| `terminal_rows` | 50 | PTY 行数 |
| `terminal_cols` | 200 | PTY 列数 |
| `drain_interval` | 0.05s | drain_loop select 超时 |
| `drain_read_size` | 65536 | PTY 每次读取字节数 |
| `startup_wait` | 8.0s | spawn 后首次读 JSONL 的等待 |
| `post_response_wait` | 3.0s | turn 结束后等待尾部输出 |
| `response_timeout` | 7200s | 不活动超时（每次 JSONL 活动刷新） |
| `jsonl_poll_interval` | 0.3s | turn 期间 JSONL 轮询频率 |
| `idle_poll_interval` | 1.0s | 自主轮 watcher 频率 |
| `subagent_check_interval` | 5.0s | sub-agent transcript 检查频率 |
| `inject_confirm_timeout` | 15.0s | channel 投递确认窗口 |
| `rate_limit_confirm_quiet` | 15.0s | 限额横幅假阳性抑制时间 |
| `max_sessions` | 20 | 会话池最大容量 |
| `idle_timeout` | 300.0s | LRU 空闲淘汰阈值 |
| `max_restart_attempts` | 3 | 自动恢复最大重试次数 |
| `restart_backoff_base` | 2.0 | 指数退避倍数（2s, 4s, 8s） |
| `config_dir` | `None` | 覆盖 `~/.claude`（账户切换） |
| `env_overrides` | `None` | 额外环境变量 |
| `disallowed_tools` | `None` | 工具黑名单 |
| `mcp_config_path` | `None` | 额外 MCP 服务器配置路径 |

## 10. 关键文件索引

```
src/claude_pty/
├── session.py            # Session 类 (414行) — 主异步 API，send_prompt/idle_watcher/auto-resume
├── pty_process.py        # PTYProcess 类 (410行) — PTY 生命周期，spawn/drain_loop/pretrust
├── jsonl_reader.py       # JsonlReader 类 — JSONL 增量读取 + 事件归一化
├── events.py             # PTYEvent + EventType 枚举 — CCM 兼容事件模型
├── config.py             # PTYConfig 数据类 (23个参数) — 所有可配置项
├── bridge.py             # BridgeHub 类 (181行) — HTTP 路由中枢
├── channel_server.py     # MCP server 入口 (365行) — claude-pty-channel CLI
├── pool.py               # SessionPool 类 (152行) — LRU 会话池管理
├── subagents.py          # SubagentTracker 类 — 原生 Agent/Task/Monitor 追踪
├── exceptions.py         # SessionError, PoolExhaustedError 等异常定义
├── _env.py               # build_clean_env() — 子进程环境变量清理
└── adapters/
    ├── base.py           # BasePTYBackend (抽象基类)
    └── ccm.py            # CCMBackend + _PTYProcessProxy — CCM 下游集成适配

文档:
├── README.md             # 用户文档：快速开始、API、配置
├── CLAUDE.md             # 开发指南：架构、任务工作流
├── PROGRESS.md           # 事故报告 + 经验教训
├── TODO.md               # 路线图
└── docs/
    ├── pty-solution.md           # 完整技术设计文档
    ├── pty-interactive-mode.md   # 交互模式研究
    └── pty-open-source-research.md  # 开源方案调研
```

## 11. 总结

> **PTY 框架把 Claude Code 从"一次性命令行工具"变成"持久服务"**，通过三个层面实现降本增效：
>
> 1. **KV cache 热复用**：进程常驻内存，多轮对话 token 成本从 O(n²) 降到 O(n)，20 轮对话可节省 70-85% 的 input token 费用
> 2. **结构化协议层**：MCP channel 注入 + JSONL 轮询，取代不可靠的终端文本解析，支持可靠的消息投递确认和错误检测
> 3. **生产级韧性**：三重撞限校验、注入确认回退、自动崩溃恢复、会话池 LRU 管理，经过 6+ 次生产事故打磨
