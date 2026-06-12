# claude-pty

以 PTY 方式编程驱动 **Claude Code CLI 交互模式** 的 Python 框架。

为什么不用 `claude -p`？headless 模式每个 prompt 都要冷启动、不能复用上下文、不能中途追加输入。claude-pty 把 CC 跑在伪终端里长驻：进程保活、会话可复用、空闲会话也能被新消息唤起，同时拿到与 stream-json 同等结构化的输出事件。

## 架构

PTY 只当宿主，消息走协议层：

```
你的程序
   │ send_prompt()
   ▼
Session ──── BridgeHub (localhost HTTP)
   │              │ inject
   │              ▼
PTYProcess    channel_server（CC 以 MCP server 子进程加载）
   │              │ MCP notification
   ▼              ▼
 Claude Code CLI（交互模式，跑在 PTY 里）
   │
   ▼ 写 ~/.claude/projects/<cwd-slug>/<session_id>.jsonl
JsonlReader ──→ PTYEvent 流（与 CCM StreamParser 对齐）
```

- **输入**：默认经 BridgeHub → channel_server 注入（MCP notification），可唤起 idle 会话开新 turn；stdin（bracketed-paste）仅作 fallback。注入返回 200 ≠ CC 真的消费了——`inject_confirm_timeout`（默认 15s）内 JSONL 无活动则 stdin 重投一次。
- **输出**：轮询 session 对应的 JSONL transcript，normalize 成 `PTYEvent`。回合结束以 `system/turn_duration` 哨兵判定（交互模式每 turn 恰一条）；`isApiErrorMessage: true` 表示 turn 被 API 错误掐断，立即以错误事件收尾。
- **注入隔离**：inject 端口由 OS 分配；注入负载带目标 session_id，不匹配回 409——防同机多宿主串话。
- **启动免对话框**：spawn 前预写 `.claude.json` 的 trust 条目和 `hasCompletedOnboarding/theme`，drain loop 兜底自动应答 `Enter to confirm` 类提示。
- **撞限检测**：JSONL 结构化 `rate_limit_event` 立即可信；PTY 屏幕横幅单独不可信（对话正文里出现限流字样会误中），需 turn 内零 JSONL 输出且再静默 `rate_limit_confirm_quiet`（默认 15s）才确认。

## 安装

```bash
# 作为 git 依赖（下游用法）
uv add "claude-pty @ git+https://github.com/zjw49246/Claude-Code-PTY.git"

# 本地开发
git clone https://github.com/zjw49246/Claude-Code-PTY.git && cd Claude-Code-PTY
uv sync --extra dev
```

无运行时第三方依赖（仅标准库），要求 Python ≥ 3.11，机器上需有可用的 `claude` CLI。

## 快速开始

### 单会话

```python
import asyncio
from claude_pty import Session

async def main():
    session = Session(cwd="/path/to/project")
    await session.start()
    async for event in session.send_prompt("总结一下这个项目"):
        print(event.event_type, event.content)
    await session.stop()

asyncio.run(main())
```

`send_prompt` 返回 `PTYEvent` 异步迭代器，事件类型见 `events.EventType`（`message` / `thinking` / `tool_use` / `tool_result` / `result` / `session_crashed` …），字段与 CCM StreamParser 输出对齐。

其他常用 API：

- `session.send_interrupt()` — 发 Esc 中断当前 turn
- `session.inject(content)` — 不开 turn 的纯通道注入
- `session.migrate_session(new_config_dir)` — 切换账号（换 `config_dir` 后 `--resume` 原会话）
- `session.on_permission_request(handler)` / `resolve_permission(...)` — 权限请求回调与外部裁决
- `Session(..., resume_existing=True)` — 恢复磁盘上已存在的 CC 会话（用 `--resume` 而非 `--session-id`）

### 会话池

```python
from claude_pty import SessionPool, BridgeHub

bridge = BridgeHub()
bridge.start()
pool = SessionPool(bridge=bridge, max_sessions=20)
session = await pool.get_or_create(cwd="/path/to/project")
# pool.drain_idle() / pool.stop_all() / pool.stats()
```

### 接入上层系统（adapter）

继承 `adapters.base.BasePTYBackend`，覆写 `build_config` / `on_event` / `on_exit`，即可把事件流接到任意宿主。`adapters/ccm.py`（`CCMBackend`）是接入 Claude-Code-Manager 的完整参考实现。

## 配置

所有调参集中在 `PTYConfig`（`src/claude_pty/config.py`），常用项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `claude_binary` | `claude` | CLI 路径 |
| `config_dir` | None | 账号目录（`CLAUDE_CONFIG_DIR`），多账号切换用 |
| `dangerously_skip_permissions` | True | 跳过权限确认 |
| `response_timeout` | 1800s | 单 turn 超时 |
| `inject_confirm_timeout` | 15s | 注入后无 JSONL 活动则 stdin 重投 |
| `rate_limit_confirm_quiet` | 15s | 横幅限流的静默确认窗口 |
| `max_sessions` / `idle_timeout` | 20 / 300s | 池容量与空闲驱逐 |

## 测试

```bash
uv run pytest          # 全量
uv run pytest tests/test_session.py -k inject   # 局部
```

设计文档在 `docs/`（`pty-solution.md`、`pty-interactive-mode.md` 等），历史经验教训见 `PROGRESS.md`。

## 下游

本仓库被 **elastic-agent**（`[pty]` extra）与 **CCM**（Claude-Code-Manager）以 git rev pin 依赖。合入涉及对外接口/行为的改动后，需在下游 `uv lock --upgrade-package claude-pty && uv sync` 手动级联（git 依赖不会自动浮动），详见 CLAUDE.md。

## 项目结构

```
src/claude_pty/
├── session.py         # 高层会话：PTYProcess + JsonlReader + 注入/限流/迁移
├── pty_process.py     # PTY 进程管理：spawn、drain loop、启动应答、横幅扫描
├── jsonl_reader.py    # JSONL transcript 轮询 → PTYEvent
├── events.py          # PTYEvent / EventType（对齐 CCM StreamParser）
├── config.py          # PTYConfig
├── bridge.py          # BridgeHub：宿主侧 HTTP 枢纽（注入转发、回执、权限）
├── channel_server.py  # claude-pty-channel：CC 加载的 MCP server 入口
├── pool.py            # SessionPool：多会话生命周期
└── adapters/          # BasePTYBackend + CCMBackend
```
