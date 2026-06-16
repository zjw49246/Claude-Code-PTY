# PTY 框架技术方案总结

## 核心思路

PTY 框架的本质是：**用一个持久的交互式 Claude Code 进程替代每次调用都要冷启动的 `-p` 模式**。

Claude Code 有两种使用方式：

- **`-p` 模式（headless）**：每条消息 = 启动进程 → 从磁盘 JSONL 重放历史 → 重建 KV cache → 处理 → 退出（cache 丢弃）
- **PTY 模式**：启动一次，常驻内存，后续消息通过 MCP channel 注入，KV cache 热复用

## 架构概览

```
你的程序 → Session.send_prompt()
              │
              ├─→ BridgeHub (HTTP) → channel_server (MCP notification) → Claude Code
              │                                                            ↓
              └─→ PTY stdin（fallback）                              写 JSONL
                                                                        ↓
                                                    JsonlReader ← 轮询 JSONL → PTYEvent
```

- **输入**：优先走 MCP channel 注入（结构化），失败回退到 PTY stdin（bracketed-paste）
- **输出**：轮询 `~/.claude/projects/<cwd>/<session_id>.jsonl`，不做终端文本解析
- **PTY 本身只管**：进程保活、启动对话框自动应答、Esc 中断、输出活动信号

## PTY vs `-p` 对比

| 维度 | `-p` 模式 | PTY 模式 |
|---|---|---|
| 每轮启动 | 冷启动 + 重放全部历史 | 进程常驻，零启动开销 |
| KV cache | 每轮丢弃，下轮重建 | 跨轮保留 |
| 第二轮延迟 | 30-60秒（随对话变长更慢） | ~7-8秒 |
| 中断能力 | SIGINT 杀进程 | Esc 软中断，保留会话 |
| 多轮对话成本 | O(n) 每轮重放全部 | O(1) 增量 |
| 工具状态 | 每轮丢失 | 跨轮保留 |

## 重点：省钱分析

### KV Cache 热复用——核心省钱机制

理解省钱需要先理解 Claude API 的计费方式：

1. **`-p` 模式下**：每轮对话 = 重新发送全部历史 token 到 API。虽然 API 端有 prompt caching（缓存命中可降价），但 `-p` 每次冷启动，**进程级别的 KV cache 完全丢失**，cache 命中率取决于 Anthropic 服务端是否还保留了上次的缓存（5 分钟 TTL），超过 5 分钟就全价重新计算。

2. **PTY 模式下**：Claude Code 进程不退出，**本地 KV cache 常驻内存**。连续对话时：
   - 历史部分的 token 不需要重新计算（cache 命中）
   - 只需要处理新增的增量内容
   - 即使 API 端 prompt cache 过期，本地 cache 依然有效

3. **实际省钱效果**：
   - **对话越长越省**——10 轮对话，`-p` 要重放 1+2+3+...+10 轮的全部内容，PTY 每轮只付增量
   - **迭代越快越省**——频繁交互（如 coding agent 反复尝试）下，`-p` 的重放开销极其浪费
   - **工具状态保留也间接省钱**——不需要重新初始化上下文（比如重新读文件、重新理解代码），减少 token 消耗

### 粗略量级估算

一个 20 轮的编程任务，假设每轮平均上下文 50K tokens：

- **`-p` 模式**：50K × 20 = 1M input tokens（全部按输入价计费，cache miss 时全价）
- **PTY 模式**：50K 首轮 + 19 轮增量 ≈ 大幅减少（cache 命中部分价格降为 1/10）

### 其他间接省钱点

- 工具状态跨轮保留，避免重复读文件、重建上下文
- 会话池复用，多任务共享进程开销
- 崩溃自恢复避免从头重来

## 工程亮点

### 注入确认机制

inject 返回 HTTP 200 不代表 Claude Code 真的消费了消息。PTY 会在 15 秒内监控 JSONL 是否有活动，无活动则通过 stdin 重新投递，防止"静默丢弃"。

### 撞限检测三重校验

| 信号 | 可信度 | 说明 |
|---|---|---|
| JSONL `rate_limit_event` | 完全可信 | 结构化信号，立即触发 |
| PTY 横幅扫描 | 需交叉验证 | 渲染的对话内容也可能包含限额文本 |
| 静默超时（15s） | 辅助确认 | 横幅 + 无 JSONL 输出 + 15s 静默 = 真限额 |

### 会话生命周期管理

- **会话池**：LRU 管理，idle 300s 自动回收
- **热复用**：同 session_id 直接返回已有进程，零启动
- **冷恢复**：`claude --resume <session_id>` 从 JSONL 恢复历史
- **崩溃重试**：指数退避（2s, 4s, 8s），最多 3 次

### 启动对话框自动应答

PTY spawn 前预写 `.claude.json`：
- `hasCompletedOnboarding: true`（跳过 theme picker）
- `hasTrustDialogAccepted: true`（跳过信任确认）
- drain loop 兜底匹配 `Enter to confirm` 等残留对话框

## 关键文件索引

```
src/claude_pty/
├── session.py          # Session 类——主要异步 API
├── pty_process.py      # PTYProcess 类——PTY 进程生命周期
├── jsonl_reader.py     # JsonlReader 类——JSONL 轮询 + 事件归一化
├── events.py           # PTYEvent + EventType 枚举
├── config.py           # PTYConfig 配置数据类
├── bridge.py           # BridgeHub 类——HTTP 路由中枢
├── channel_server.py   # MCP server 入口——claude-pty-channel CLI
├── pool.py             # SessionPool 类——LRU 会话池
├── subagents.py        # SubagentTracker——原生 Agent/Task/Monitor 追踪
└── adapters/
    └── ccm.py          # CCMBackend——下游 CCM 集成适配
```

## 一句话总结

> PTY 框架把 Claude Code 从"一次性命令行工具"变成"持久服务"，通过 KV cache 热复用实现多轮对话显著降本，通过 MCP channel + JSONL 协议层实现可靠的结构化输入输出。
