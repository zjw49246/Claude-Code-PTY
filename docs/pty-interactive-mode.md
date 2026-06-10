# PTY 交互模式运行 Claude Code — 技术方案

通过 PTY（伪终端）以交互模式运行 Claude Code CLI 的 Python 框架。与 `-p` 非交互模式不同，PTY 模式启动一个**长驻的交互式进程**，通过 PTY 发送 prompt，通过 session JSONL 文件读取结构化响应，可选通过 Channels MCP 支持中途消息注入和权限中继。

## 核心架构

```
你的后端 (FastAPI / asyncio)
│
├── Session.send_prompt("实现登录功能")         [async generator, 实时 yield 事件]
│     │
│     ├── PTYProcess                             [Layer 1: PTY 进程管理]
│     │     ├── master_fd ──write──→ slave (stdin) ──→ Claude Code 交互进程
│     │     │     逐字符写入，高斯随机 30-70ms 间隔
│     │     │
│     │     └── Drain Thread ←──read── master_fd    (独立 daemon 线程，50ms 间隔持续排空)
│     │
│     ├── JsonlReader                            [Layer 2: JSONL 读取 + 事件标准化]
│     │     ├── 每 300ms 轮询 JSONL session 文件
│     │     ├── 行缓冲：保留不完整尾行到下次拼接
│     │     └── normalize() → PTYEvent (CCM StreamParser 兼容格式)
│     │
│     └── CC 进程自动写入 JSONL ─────→ ~/.claude/projects/{hash}/{session_id}.jsonl
│
├── SessionPool                                  [Layer 4: LRU 会话池]
│     └── get_or_create() / 淘汰策略 / stats()
│
└── [可选] Channels MCP 层
      ├── BridgeHub        (中心路由, localhost HTTP)
      ├── channel_server   (每 session 一个 MCP 服务器, stdio JSON-RPC)
      ├── inject()         (中途消息注入)
      ├── on_reply()       (CC 主动回复)
      └── permission relay (权限中继: allow/deny)
```

## 与 `-p` 模式的对比

| | `-p` 非交互模式 | PTY 交互模式 |
|--|--|--|
| 进程生命周期 | 每轮一个新进程（5-8 秒启动） | 一个长驻进程，多轮复用（即时响应） |
| 多轮上下文 | 需要 `--resume` + session_id | 天然连续，同一进程内 |
| 输出格式 | `--output-format stream-json` (stdout) | session JSONL 文件 (磁盘) |
| 中途注入消息 | 不可能 | Channels MCP 支持 |
| 权限控制 | 全局 skip 或手动 | 程序化 allow/deny（权限中继） |
| 并发管理 | 手动管理进程 | LRU 会话池（最多 20 并发） |
| TUI | 无 | 完整渲染（由 drain 线程排空，不解析） |
| 中途中断 | SIGTERM 杀进程 | ESC 键 via PTY |
| 崩溃恢复 | 手动重启 | 自动 --resume + 指数退避（最多 3 次） |

## 四层架构

### Layer 1: PTYProcess (`pty_process.py`, 258 行)

单个 Claude Code PTY 进程的低级封装。

**核心职责：**
- 创建 PTY 对，启动 CC 进程
- 独立 daemon 线程持续排空 PTY 缓冲区
- 逐字符发送 prompt
- 进程生命周期管理（启动/停止/检测死亡）

```python
class PTYProcess:
    def __init__(self, cwd, session_id=None, config=None, on_death=None,
                 channel_inject_port=None, bridge_port=None): ...

    def spawn(self, resume_session_id=None):
        """创建 PTY + 启动 CC + 启动 drain 线程"""

    def send_prompt(self, text):
        """逐字符写入 + 回车"""

    def send_interrupt(self):
        """发送 ESC 键"""

    def stop(self, timeout=5):
        """SIGTERM → 轮询 → SIGKILL"""

    @property
    def is_alive(self) -> bool: ...
    @property
    def jsonl_path(self) -> str: ...
    @property
    def channels_enabled(self) -> bool: ...
```

**spawn() 流程：**

```python
def spawn(self, resume_session_id=None):
    # 1. 如果启用 Channels，写入 .mcp.json 配置
    if self._channel_inject_port:
        self._setup_mcp_config()

    # 2. 创建 PTY 对，设置终端大小
    master, slave = pty.openpty()
    winsize = struct.pack('HHHH', 50, 200, 0, 0)  # rows, cols
    fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

    # 3. 清洗环境变量，启动子进程
    env = build_clean_env(self.config)
    cmd = self._build_command(resume_session_id)
    self.proc = subprocess.Popen(
        cmd, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, close_fds=True, cwd=self.cwd, env=env,
    )
    os.close(slave)  # 父进程不需要 slave 端
    self.master_fd = master

    # 4. 启动 drain 线程
    self._drain_thread = threading.Thread(target=self._drain_loop, daemon=True)
    self._drain_thread.start()
```

**构建的命令行：**

```bash
# 基本模式
claude --dangerously-skip-permissions --session-id {uuid}

# 恢复模式（崩溃后）
claude --dangerously-skip-permissions --resume {uuid}

# Channels 模式
claude --dangerously-skip-permissions --session-id {uuid} \
       --dangerously-load-development-channels server:pty-bridge

# 可选参数
--model {model}  --effort {effort}
```

### Layer 2: JsonlReader (`jsonl_reader.py`, 332 行)

读取 CC session JSONL 文件并标准化为 CCM 兼容事件。

**核心职责：**
- 增量读取 JSONL 文件，行缓冲处理 partial write
- `normalize()` 将交互模式 JSONL 转换为 PTYEvent（兼容 CCM StreamParser 输出格式）
- 检测响应是否完成（`stop_reason == "end_turn"`）

```python
class JsonlReader:
    def __init__(self, path): ...

    def read_new_messages(self) -> list[dict]:
        """增量读取新 JSONL 行，保留不完整尾行到 _buffer"""

    def normalize(self, raw) -> list[PTYEvent]:
        """将原始 JSONL dict 转换为标准 PTYEvent 列表"""

    def is_response_complete(self, raw) -> bool:
        """检查 type==assistant 且 stop_reason==end_turn"""
```

**normalize() 处理的消息类型：**

| JSONL type | 生成的 PTYEvent |
|------------|----------------|
| `system` (首条) | `system_init` |
| `assistant` → text block | `message` (role=assistant) |
| `assistant` → thinking block | `thinking` (role=assistant) |
| `assistant` → tool_use block | `tool_use` (role=assistant) |
| `user` → tool_result | `tool_result` (role=tool) |
| `result` | `result` (含 cost_usd, context_usage) |
| `queue-operation`, `ai-title` 等 | 跳过（噪声事件） |

**PTYEvent.to_dict() 输出格式（与 CCM StreamParser.parse_line() 完全一致）：**

```json
{
    "event_type": "message",
    "role": "assistant",
    "content": "好的，我来实现...",
    "tool_name": null,
    "tool_input": null,
    "tool_output": null,
    "raw_json": "...",
    "is_error": false,
    "timestamp": "2026-06-10T...",
    "session_id": "uuid...",
    "cost_usd": null,
    "context_usage": null
}
```

### Layer 3: Session (`session.py`, 269 行)

高级会话抽象，组合 PTYProcess + JsonlReader。

```python
class Session:
    def __init__(self, cwd, session_id=None, config=None,
                 bridge=None, channel_inject_port=None): ...

    async def start(self): ...
    async def stop(self): ...

    async def send_prompt(self, text, timeout=None) -> AsyncIterator[PTYEvent]:
        """核心 API：发送 prompt，实时 yield 事件直到 end_turn"""

    async def inject(self, content, meta=None) -> bool:
        """[Channels] 中途注入消息"""

    def on_permission_request(self, handler):
        """[Channels] 注册权限请求回调"""

    async def resolve_permission(self, request_id, behavior="allow") -> bool:
        """[Channels] 响应权限请求"""

    async def send_interrupt(self): ...
```

**send_prompt() 内部流程：**

```python
async def send_prompt(self, text, timeout=None):
    async with self._send_lock:  # 同一 session 串行处理
        # 1. 如果进程已死，自动恢复
        if not self._process.is_alive:
            await self._auto_resume()

        # 2. 逐字符写入 prompt（在线程池中执行，不阻塞 event loop）
        await loop.run_in_executor(None, self._process.send_prompt, text)

        # 3. 轮询 JSONL 文件，yield 事件
        while not response_complete and time.monotonic() < deadline:
            if not self._process.is_alive:
                yield PTYEvent(type=SESSION_CRASHED, ...)
                break

            messages = await loop.run_in_executor(None, self._reader.read_new_messages)
            for raw in messages:
                for event in self._reader.normalize(raw):
                    yield event  # 实时逐条返回
                if self._reader.is_response_complete(raw):
                    response_complete = True
                    break

            await asyncio.sleep(0.3)  # 让出 event loop

        # 4. 等待 CC 回到输入状态
        await asyncio.sleep(config.post_response_wait)  # 默认 3 秒
```

**自动崩溃恢复（_auto_resume）：**

```python
async def _auto_resume(self):
    if self._restart_count >= config.max_restart_attempts:  # 默认 3
        raise SessionError("exceeded max restart attempts")

    self._restart_count += 1
    backoff = 2 ** self._restart_count  # 2s, 4s, 8s
    await asyncio.sleep(backoff)

    # start() 检测到 restart_count > 0，会用 --resume 而不是 --session-id
    await self.start()
```

### Layer 4: SessionPool (`pool.py`, 134 行)

LRU 会话池，管理多个并发 Session。

```python
class SessionPool:
    def __init__(self, config=None, bridge=None): ...

    async def get_or_create(self, session_id, cwd, config_override=None,
                            channels=False) -> Session:
        """复用已有 session 或创建新的。达到上限时自动淘汰。"""

    async def remove(self, session_id): ...
    async def stop_all(self): ...
    def stats(self) -> dict: ...
```

**淘汰策略：**
1. 优先淘汰超过 `idle_timeout`（默认 300 秒）的空闲 session
2. 如果都未超时，淘汰最久未使用且当前没在执行 prompt 的 session
3. 如果所有 session 都在忙，抛出 `PoolExhaustedError`

## Channels MCP 层（可选）

基础的 JSONL 模式适用于 prompt → response 的标准流程。当需要在 CC **执行过程中**注入消息或做权限控制时，启用 Channels。

### 组件

**channel_server (`channel_server.py`, 356 行)**
- CC 启动时通过 `.mcp.json` 自动加载的 MCP 服务器
- 通过 stdin/stdout 与 CC 通信（JSON-RPC 2.0 协议）
- 同时开 HTTP 端口接收外部注入请求
- 入口点：`claude-pty-channel` CLI 命令

**BridgeHub (`bridge.py`, 176 行)**
- 中心路由，管理 session_id → channel_server 端口映射
- 提供 inject()、on_reply()、on_permission_request()、resolve_permission() API
- localhost HTTP 服务器

### 消息注入流程

```
你的后端                BridgeHub              channel_server           Claude Code
   │                      │                      │                      │
   │  inject(sid, "需求变了") │                   │                      │
   ├─────────────────────→│  HTTP /inject        │                      │
   │                      ├─────────────────────→│  MCP notification    │
   │                      │                      ├─────────────────────→│
   │                      │                      │                      │ CC 在下一个工具
   │                      │                      │                      │ 调用边界读到消息
```

### 回复转发流程

```
   CC 调用 pty_bridge_reply 工具 → channel_server 收到 tools/call
   channel_server HTTP POST /reply → BridgeHub 触发 on_reply 回调 → 你的后端
```

### 权限中继流程

```
   CC 要用工具 → 发 permission_request (stdio)
   channel_server → HTTP /permission_request → BridgeHub
   BridgeHub 触发 on_permission_request 回调 → 你的后端判断 allow/deny
   你的后端调用 resolve_permission()
   BridgeHub → HTTP /permission_resolve → channel_server
   channel_server → permission notification (stdio) → CC 继续/中止
   超时 120 秒未响应 → 默认 deny
```

### 使用方式

```python
from claude_pty import SessionPool, BridgeHub

# 启动 BridgeHub
bridge = BridgeHub(port=18000)
bridge.start()

# 注册回调
bridge.on_reply(lambda sid, text: print(f"CC says: {text}"))
bridge.on_permission_request(lambda sid, req:
    bridge.resolve_permission(sid, req["request_id"], "allow")
)

# 创建 session，启用 channels
pool = SessionPool(bridge=bridge)
session = await pool.get_or_create("task-1", "/project", channels=True)

# 正常发 prompt
async for event in session.send_prompt("重构用户模块"):
    print(event)

# CC 执行中途注入
await session.inject("需求更新：还需要支持 OAuth 登录")
```

## 关键实现细节

### 1. PTY 缓冲区必须由独立线程持续排空

PTY 内核缓冲区（`N_TTY_BUF_SIZE`）仅 **4096 bytes**。CC 的 Ink TUI 单帧输出可达 10-50KB ANSI 转义序列。缓冲区满后 `write()` 阻塞，CC 进程挂起。

**解决：** 独立 daemon 线程，每 50ms `select() + os.read(65536)` 排空。数据直接丢弃（TUI 输出对我们无用），但必须持续排空。

```python
def _drain_loop(self):
    while self._running:
        try:
            r, _, _ = select.select([self.master_fd], [], [], 0.05)
            if r:
                data = os.read(self.master_fd, 65536)
                if not data:
                    self._child_dead.set()
                    break
        except OSError:
            self._child_dead.set()  # EIO = 子进程已退出
            break
```

### 2. 逐字符输入

CC 的 TUI 用 raw mode 监听按键。批量写入在第一轮可行但后续轮次会丢字符。

**解决：** 逐字符写入，高斯随机间隔（均值 50ms，标准差 20ms，钳位 10-150ms），写完等 100ms 再发回车 `\r`。

```python
def send_prompt(self, text):
    for ch in text:
        os.write(self.master_fd, ch.encode("utf-8"))
        delay = random.gauss(0.05, 0.02)
        time.sleep(max(0.01, min(0.15, delay)))
    time.sleep(0.1)
    os.write(self.master_fd, b"\r")
```

### 3. JSONL 行缓冲

JSONL 写入非原子——单条消息可能超过 `PIPE_BUF`（4096 bytes），`write()` 可能被拆分。读取时可能读到半截 JSON。

**解决：** 按 `\n` 分割，最后一段没有换行的保留到 `_buffer`，下次拼接。只解析完整行。

```python
def read_new_messages(self):
    new_data = f.read()
    self.offset += len(new_data.encode('utf-8'))
    combined = self._buffer + new_data
    lines = combined.split('\n')
    self._buffer = lines[-1]  # 不完整的尾行，保留到下次
    complete_lines = lines[:-1]  # 完整行，解析返回
```

### 4. subprocess.Popen 而不是 os.fork()

`os.fork()` 在有 asyncio event loop 或多线程的进程中会复制脏状态导致死锁。`subprocess.Popen` + `pty.openpty()` 避免了所有 fork 问题，完美兼容 asyncio。

### 5. 环境变量清洗

启动子进程前删除所有含 `CLAUDE`、`CLAUDECODE`、`AI_AGENT` 的环境变量（防止嵌套检测），设置 `TERM=xterm-256color`、`LANG=en_US.UTF-8`。

```python
def build_clean_env(config):
    env = os.environ.copy()
    for key in list(env):
        upper = key.upper()
        if any(p in upper for p in ("CLAUDE", "CLAUDECODE", "AI_AGENT")):
            del env[key]
    env.update({"TERM": "xterm-256color", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"})
    if config.config_dir:
        env["CLAUDE_CONFIG_DIR"] = config.config_dir
    return env
```

### 6. Session JSONL 文件位置

```
~/.claude/projects/{project_hash}/{session_id}.jsonl
```

`project_hash` = cwd 路径中 `/` 替换为 `-`（保留开头的 `-`）：

```
/home/ubuntu/Projects/PTY → -home-ubuntu-Projects-PTY
```

### 7. 线程/异步架构

```
asyncio event loop (你的 FastAPI 后端)
  │
  ├── Session.send_prompt()              [async generator]
  │     ├── run_in_executor → process.send_prompt()     # 线程池: 阻塞式逐字符写入
  │     ├── run_in_executor → reader.read_new_messages() # 线程池: 文件 I/O
  │     └── asyncio.sleep(0.3)                           # 让出 event loop
  │
  └── PTYProcess._drain_loop()           [独立 daemon thread, 每进程一个]
        └── select(50ms) + os.read()     # 纯线程，不碰 asyncio
```

## JSONL 消息格式

Session JSONL 文件中每行是一个 JSON 对象。关键类型：

### System 消息（启动时）

```json
{
  "type": "system",
  "subtype": "init",
  "sessionId": "uuid...",
  "version": "2.1.168",
  "tools": ["Read", "Edit", "Bash", ...],
  "model": "claude-opus-4-6"
}
```

### Assistant 消息

```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "model": "claude-opus-4-6",
    "content": [
      { "type": "thinking", "thinking": "让我分析..." },
      { "type": "text", "text": "好的，我来实现..." },
      { "type": "tool_use", "name": "Edit", "id": "toolu_...", "input": { ... } }
    ],
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 24190,
      "output_tokens": 856,
      "cache_creation_input_tokens": 24190,
      "cache_read_input_tokens": 0
    }
  },
  "sessionId": "uuid..."
}
```

### User 消息（工具结果）

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      { "type": "tool_result", "tool_use_id": "toolu_...", "content": "文件已修改" }
    ]
  },
  "sessionId": "uuid..."
}
```

### Result 消息

```json
{
  "type": "result",
  "result": "最终文本内容...",
  "is_error": false,
  "duration_ms": 15234,
  "duration_api_ms": 12100,
  "num_turns": 3,
  "session_id": "uuid...",
  "cost_usd": 0.042,
  "modelUsage": {
    "claude-opus-4-6": {
      "inputTokens": 48000,
      "outputTokens": 2400,
      "cacheReadInputTokens": 24000,
      "cacheCreationInputTokens": 24000
    }
  }
}
```

### 跳过的噪声类型

以下 JSONL 类型被 `normalize()` 跳过：`queue-operation`、`attachment`、`ai-title`、`last-prompt`、`mode`、`permission-mode`、`file-history-snapshot`。

## 配置参数

```python
@dataclass
class PTYConfig:
    claude_binary: str = "claude"
    dangerously_skip_permissions: bool = True
    default_model: str | None = None
    default_effort: str | None = None

    terminal_rows: int = 50           # PTY 终端行数
    terminal_cols: int = 200          # PTY 终端列数
    char_send_delay_mean: float = 0.05   # 逐字符输入间隔均值 (秒)
    char_send_delay_stddev: float = 0.02
    char_send_delay_min: float = 0.01
    char_send_delay_max: float = 0.15
    drain_interval: float = 0.05     # drain 线程 select 超时 (秒)
    drain_read_size: int = 65536

    startup_wait: float = 8.0        # CC 启动等待 (秒)
    post_response_wait: float = 3.0  # 响应结束后等待 (秒)
    response_timeout: float = 1800.0 # 单次 prompt 超时 (秒)
    jsonl_poll_interval: float = 0.3 # JSONL 轮询间隔 (秒)

    max_sessions: int = 20           # 会话池最大并发数
    idle_timeout: float = 300.0      # 空闲 session 淘汰阈值 (秒)

    max_restart_attempts: int = 3    # 崩溃自动恢复次数上限
    restart_backoff_base: float = 2.0 # 退避基数 (2^n 秒)

    config_dir: str | None = None    # 自定义 CLAUDE_CONFIG_DIR
```

## 异常层级

```
ClaudePTYError (基类)
├── PTYSpawnError      — 启动 PTY 进程失败
├── PTYDeadError       — 在已死进程上操作
├── SessionError       — Session 级别错误
└── PoolExhaustedError — 会话池满，无法创建新 session
```

## 事件类型

```python
class EventType(str, Enum):
    SYSTEM_INIT = "system_init"
    SYSTEM_EVENT = "system_event"
    MESSAGE = "message"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    PROCESS_EXIT = "process_exit"
    PARSE_ERROR = "parse_error"
    SESSION_STARTED = "session_started"
    SESSION_CRASHED = "session_crashed"
    SESSION_RESUMED = "session_resumed"
```

## 使用示例

### 最简用法

```python
from claude_pty import Session, PTYConfig

session = Session(cwd="/path/to/project")
await session.start()

async for event in session.send_prompt("帮我写一个 hello world"):
    print(event.event_type, event.content)

await session.stop()
```

### 多轮对话

```python
session = Session(cwd="/project")
await session.start()

# 第一轮
async for event in session.send_prompt("记住数字 42"):
    ...

# 第二轮（CC 自动延续上下文）
async for event in session.send_prompt("我说的数字是什么？"):
    ...  # CC 会回答 "42"

await session.stop()
```

### 会话池 + 并发

```python
from claude_pty import SessionPool, PTYConfig

pool = SessionPool(config=PTYConfig(max_sessions=10))

async def run_task(task_id, prompt, cwd):
    session = await pool.get_or_create(task_id, cwd)
    async for event in session.send_prompt(prompt):
        await save_to_db(task_id, event.to_dict())

await asyncio.gather(
    run_task("task-1", "实现登录", "/project"),
    run_task("task-2", "写测试", "/project"),
)
await pool.stop_all()
```

### Channels 注入 + 权限控制

```python
from claude_pty import SessionPool, BridgeHub

bridge = BridgeHub(port=18000)
bridge.start()

bridge.on_reply(lambda sid, text: print(f"CC: {text}"))
bridge.on_permission_request(lambda sid, req:
    bridge.resolve_permission(sid, req["request_id"],
        "allow" if req["tool_name"] in ("Read", "Bash") else "deny")
)

pool = SessionPool(bridge=bridge)
session = await pool.get_or_create("task-1", "/project", channels=True)

async for event in session.send_prompt("重构用户模块"):
    print(event)

await session.inject("需求更新：还需要支持 OAuth")
```

### CCM 集成适配器

```python
# CCM 侧写一个适配器，现有 _process_event 零改动
from claude_pty import SessionPool

class PTYAdapter:
    def __init__(self, pool: SessionPool):
        self.pool = pool

    async def send_prompt(self, task_id, instance_id, session_id, prompt, cwd):
        session = await self.pool.get_or_create(session_id, cwd=cwd)
        async for event in session.send_prompt(prompt):
            # event.to_dict() 格式 == StreamParser.parse_line() 格式
            await self._process_event(instance_id, task_id, event.to_dict())
```

## 并发部署注意事项

| 资源 | 默认限制 | 说明 |
|------|----------|------|
| PTY 数量 | `/proc/sys/kernel/pty/max` = 4096 | 每个 session 占 1 对 PTY |
| 文件描述符 | `ulimit -n` = 1024（软限制） | 每 session 约 5 个 fd |
| 内存 | — | 每个 CC（Node.js）约 100-300MB |
| API 限额 | 按 API key/组织 | 多 session 共享，注意 tokens-per-minute |

## 安全注意事项

- `--dangerously-skip-permissions` 下 CC 有不受限的文件系统读写执行权限
- 推荐在容器中运行，配合 `--network none`
- 环境变量清洗避免泄露敏感信息
- 权限中继（Channels 模式）可替代全局 skip，实现细粒度控制

## 故障排查

| 故障 | 症状 | 原因 | 恢复 |
|------|------|------|------|
| PTY 缓冲区满 | CC 进程挂起 | drain 线程未运行 | 检查线程是否存活 |
| 进程崩溃 | SESSION_CRASHED 事件 | OOM / 网络断开 / 内部错误 | 自动 --resume（最多 3 次） |
| 输入丢失 | 无 user 消息 | 批量写入导致 | 已用逐字符解决 |
| JSONL 半截 | JSONDecodeError | partial write | 行缓冲已处理 |
| 启动超时 | JSONL 文件未创建 | CC 初始化慢 | 增大 startup_wait |
| 第二轮吞字符 | 响应不完整 | post_response_wait 不够 | 增大等待时间 |

## 测试

72 个测试全部通过（67 单元 + 5 集成）：

| 测试文件 | 数量 | 覆盖 |
|---------|------|------|
| test_env.py | 6 | 环境变量清洗 |
| test_config.py | 2 | 配置默认值 |
| test_events.py | 5 | 事件模型 |
| test_jsonl_reader.py | 21 | JSONL 读取 + normalize |
| test_pty_process.py | 9 | 命令构建 + 路径 + MCP 配置 |
| test_bridge.py | 8 | BridgeHub HTTP 路由 |
| test_channel_server.py | 3 | 权限 resolve |
| test_pool.py | 8 | LRU 淘汰逻辑 |
| test_integration.py | 5 | 真实 CC 进程：spawn/stop、JSONL 创建、prompt→response、多轮对话、事件格式 |

运行：`python -m pytest tests/ -v`
