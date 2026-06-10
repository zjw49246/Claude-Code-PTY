# PTY 交互模式运行 Claude Code

通过 PTY（伪终端）以交互模式运行 Claude Code CLI 的技术方案。与 `-p` 非交互模式不同，PTY 模式启动一个**长驻的交互式进程**，通过 stdin 发送 prompt，通过 session JSONL 文件读取结构化响应。

## 核心原理

```
Python 后端
│
├── PTY master fd ──write──→ PTY slave (stdin)  ──→ Claude Code 交互进程
│                                                       │
├── Drain Thread ←──read──── PTY master fd (stdout) ────┘  (持续排空，防止阻塞)
│                                                       │
│                                                       └─→ JSONL 转录文件 (实时写入)
│                                                                │
├── HTTP Stop Hook ←─── POST 通知 ───────────────────────────────┤  (主检测方式)
│                                                                │
└── JSONL File Watcher ←─────────────────────────────────────────┘  (备用检测方式)
     读取结构化 JSON 消息
```

PTY 是操作系统内核级别的终端抽象。Claude Code 通过 `isatty(stdout)` 判断运行模式——PTY slave 是一个真实的 TTY 设备（`/dev/pts/N`），因此 Claude Code 以完整的交互模式启动，具备全部功能：TUI 界面、工具调用、session 持久化、上下文延续。

## 与 `-p` 模式的对比

| | `-p` 非交互模式 | PTY 交互模式 |
|--|--|--|
| 进程生命周期 | 每轮一个新进程 | 一个长驻进程，多轮复用 |
| 多轮上下文 | 需要 `--resume` + session_id | 天然连续，同一进程内 |
| 输出格式 | `--output-format stream-json` (stdout) | session JSONL 文件 (磁盘) |
| TUI | 无 | 完整渲染（但可忽略） |
| 中途中断 | SIGTERM 杀进程 | ESC 键 / Ctrl+C via PTY |
| 工具调用 | 全部可用 | 全部可用 |
| Session 持久化 | 可选 | 自动 |

## 前置条件

### Claude Code 用户设置

在 `~/.claude/settings.json` 中配置：

```json
{
  "skipDangerousModePermissionPrompt": true
}
```

这会跳过每次启动时的 workspace trust 确认对话框。不配置的话需要在 PTY 中自动发送 Enter 来确认。

### 环境变量清理

启动子进程前必须清除以下环境变量，避免嵌套 session 检测或继承父进程状态：

```python
VARS_TO_CLEAN = [
    'CLAUDECODE',
    'CLAUDE_CODE',
    'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_EXECPATH',
    'CLAUDE_EFFORT',
    'CLAUDE_AGENT_SDK_VERSION',
    'CLAUDE_CODE_AGENT',
    'CLAUDE_CODE_SESSION_NAME',
    'CLAUDE_CODE_SESSION_LOG',
    'CLAUDE_CODE_SIMPLE',
    'CLAUDE_JOB_DIR',
    'AI_AGENT',
]
```

同时必须**显式设置**以下变量：

```python
VARS_TO_SET = {
    'TERM': 'xterm-256color',
    'LANG': 'en_US.UTF-8',      # 确保 UTF-8 编码，否则中文等多字节字符可能异常
    'LC_ALL': 'en_US.UTF-8',
}
```

### 环境变量安全

子进程会继承父进程的完整环境。启动前应只保留必要变量，避免泄露敏感信息（API keys、数据库凭据等）到 Claude Code 及其调用的工具命令中。

```python
def build_clean_env(cwd: str, session_id: str) -> dict:
    """构建干净的子进程环境变量"""
    env = os.environ.copy()

    for key in list(env.keys()):
        upper = key.upper()
        if any(x in upper for x in ['CLAUDE', 'CLAUDECODE', 'AI_AGENT']):
            del env[key]

    env['TERM'] = 'xterm-256color'
    env['LANG'] = 'en_US.UTF-8'
    env['LC_ALL'] = 'en_US.UTF-8'

    return env
```

## 实现参考

### PTY 进程管理

使用 `subprocess.Popen` + `pty.openpty()` 代替 `os.fork()`。`os.fork()` 在多线程或 asyncio 环境下会复制脏状态导致死锁（Python 3.12+ 已发出 DeprecationWarning），而 `subprocess.Popen` 内部处理了这些问题。

```python
import pty, os, select, fcntl, termios, struct, uuid, time, json, signal
import subprocess, threading, random

class PTYInstance:
    """管理一个长驻的 Claude Code 交互式 PTY 进程"""

    def __init__(self, cwd: str, session_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())
        self.cwd = cwd
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._drain_thread: threading.Thread | None = None
        self._running = False
        self._child_dead = False

    def spawn(self):
        master, slave = pty.openpty()

        # 设置终端尺寸（rows, cols, xpixel, ypixel）
        winsize = struct.pack('HHHH', 50, 200, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

        env = build_clean_env(self.cwd, self.session_id)

        self.proc = subprocess.Popen(
            [
                'claude',
                '--dangerously-skip-permissions',
                '--session-id', self.session_id,
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
            cwd=self.cwd,
            env=env,
        )
        os.close(slave)
        self.master_fd = master

        # 启动独立的 drain 线程
        self._running = True
        self._child_dead = False
        self._drain_thread = threading.Thread(
            target=self._drain_loop, daemon=True
        )
        self._drain_thread.start()

    def _drain_loop(self):
        """独立线程：持续排空 PTY 缓冲区

        PTY 内核缓冲区仅约 4KB，Claude Code TUI 单帧输出可达 10-50KB。
        如果不及时读取，缓冲区满后 Claude Code 的 stdout 写入会阻塞，
        整个进程挂起。必须用独立线程以 ~50ms 间隔持续排空。
        """
        while self._running:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
                if r:
                    data = os.read(self.master_fd, 65536)
                    if not data:
                        self._child_dead = True
                        break
            except OSError:
                # EIO = slave 端已关闭，即子进程已退出
                self._child_dead = True
                break

    def send_prompt(self, text: str):
        """逐字符发送 prompt 并按 Enter 提交

        Claude Code 的 TUI 基于 Ink（React for CLI），使用 raw mode 处理键盘事件。
        逐字符发送（随机 30-70ms 间隔）在所有轮次都稳定工作。
        一次性 bulk write 在首轮可行但后续轮次可能丢字符。
        """
        for ch in text:
            os.write(self.master_fd, ch.encode())
            delay = random.gauss(0.05, 0.02)
            time.sleep(max(0.01, min(0.15, delay)))
        time.sleep(0.1)
        os.write(self.master_fd, b'\r')

    def send_interrupt(self):
        """发送 ESC 键中断当前操作"""
        os.write(self.master_fd, b'\x1b')

    @property
    def is_alive(self) -> bool:
        """检查子进程是否仍在运行"""
        if self._child_dead:
            return False
        if self.proc:
            return self.proc.poll() is None
        return False

    def stop(self):
        """优雅停止 PTY 进程：先 SIGTERM，等待最多 2 秒，再 SIGKILL"""
        self._running = False

        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            for _ in range(20):  # 2 秒，每 100ms 检查一次
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                self.proc.kill()
                self.proc.wait()

        if self._drain_thread and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=2)

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    @property
    def jsonl_path(self) -> str:
        """Session JSONL 转录文件路径"""
        project_hash = self.cwd.replace('/', '-')
        return os.path.expanduser(
            f'~/.claude/projects/{project_hash}/{self.session_id}.jsonl'
        )
```

### JSONL 响应追踪

Claude Code 将每条消息实时写入 session JSONL 文件，格式与 `-p --output-format stream-json` 高度一致。

**关键修复：行缓冲处理。** JSONL 写入不保证原子性——单条 assistant 消息可能超过 4KB，`write()` 可能被拆分。如果在写入中途读取，会拿到不完整的 JSON 行。旧实现中 `offset` 会越过残行，导致**消息永久丢失**。新实现保留未完成的尾行到下次拼接。

```python
class JsonlTracker:
    """追踪 session JSONL 文件，读取新增的结构化消息"""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self._buffer = ''  # 未完成的行尾缓冲

    def read_new_messages(self) -> list[dict]:
        """读取自上次调用以来的新消息（安全处理不完整行）"""
        if not os.path.exists(self.path):
            return []

        with open(self.path, encoding='utf-8') as f:
            f.seek(self.offset)
            new_data = f.read()

        if not new_data:
            return []

        combined = self._buffer + new_data
        lines = combined.split('\n')

        # 最后一个元素可能是不完整的行（或空字符串）
        self._buffer = lines[-1]
        complete_lines = lines[:-1]

        # offset 前进到已消费数据的位置，保留 buffer 未消费的部分
        self.offset += len(new_data.encode('utf-8'))
        self.offset -= len(self._buffer.encode('utf-8'))

        results = []
        for line in complete_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                results.append(json.loads(stripped))
            except json.JSONDecodeError:
                pass  # 真正损坏的行，跳过
        return results

    def wait_for_response(self, pty_instance: 'PTYInstance',
                          timeout: float = 120) -> dict | None:
        """阻塞等待 assistant 回复完成（stop_reason == end_turn）

        drain 已由独立线程处理，这里只需轮询 JSONL 文件。
        同时检查子进程存活状态，避免无限等待已崩溃的进程。
        """
        start = time.time()
        while time.time() - start < timeout:
            # 检查进程是否意外退出
            if not pty_instance.is_alive:
                return None

            for msg in self.read_new_messages():
                if (msg.get('type') == 'assistant' and
                        msg.get('message', {}).get('stop_reason') == 'end_turn'):
                    return msg

            time.sleep(0.3)
        return None
```

### 完整的交互流程

```python
# 1. 创建 PTY 实例并启动（drain 线程自动启动）
pty_inst = PTYInstance(cwd="/path/to/project")
pty_inst.spawn()

# 2. 等待 Claude Code 启动（TUI 渲染完成）
time.sleep(8)

# 3. 创建 JSONL 追踪器
tracker = JsonlTracker(pty_inst.jsonl_path)
tracker.read_new_messages()  # 跳过启动阶段的初始化消息

# 4. 发送第一条 prompt
pty_inst.send_prompt("实现用户登录功能")
response = tracker.wait_for_response(pty_inst, timeout=120)

# 5. 提取 assistant 回复内容
if response:
    for block in response['message']['content']:
        if block.get('type') == 'text':
            print(block['text'])
        elif block.get('type') == 'tool_use':
            print(f"[调用工具: {block['name']}]")

# 6. 等待 TUI 恢复输入状态后发送后续 prompt（上下文自动延续）
time.sleep(3)
pty_inst.send_prompt("加上单元测试")
response2 = tracker.wait_for_response(pty_inst, timeout=120)

# 7. 结束时停止进程
pty_inst.stop()
```

## JSONL 消息格式

Session JSONL 文件中每行是一个 JSON 对象，关键消息类型：

### User 消息

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": "实现用户登录功能"
  },
  "uuid": "9a1a5a35-...",
  "timestamp": "2026-05-14T13:42:41.677Z",
  "sessionId": "007c1594-...",
  "entrypoint": "cli",
  "userType": "external",
  "permissionMode": "bypassPermissions",
  "cwd": "/path/to/project"
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
      { "type": "text", "text": "我来实现登录功能..." },
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
  "uuid": "468a2b1c-...",
  "timestamp": "2026-05-14T14:09:51.501Z",
  "sessionId": "007c1594-...",
  "entrypoint": "cli"
}
```

### 关键字段

| 字段 | 说明 |
|------|------|
| `message.stop_reason` | `"end_turn"` 表示回复完成，可以发送下一条 prompt |
| `message.content[]` | 内容块数组，包含 `text`、`tool_use`、`tool_result` 等类型 |
| `message.usage` | token 用量统计 |
| `message.model` | 使用的模型 |
| `sessionId` | 当前 session ID |

## 实现要点

### 1. PTY 缓冲区必须由独立线程持续排空

PTY 内核缓冲区（N_TTY_BUF_SIZE）仅 **4096 bytes**。Claude Code 的 TUI 基于 Ink 框架，渲染输出包含大量 ANSI 转义序列（24-bit RGB 颜色、光标定位、全屏重绘），单帧可达 **10-50KB**。如果不读取 master fd，缓冲区满后 `write()` 阻塞，进程挂起。

**必须用独立线程以 ~50ms 间隔持续排空，不能与 JSONL 轮询交替执行。** 旧方案的 `drain_pty()` + `time.sleep(0.5)` 交替模式在重输出场景下不可靠。

### 2. 逐字符输入更可靠

Claude Code 的 TUI 通过 raw mode 处理键盘事件。逐字符发送（随机 30-70ms 间隔）在所有轮次都稳定工作。一次性 bulk write 在第一轮可行但后续轮次可能丢失。

加入高斯随机抖动使输入节奏更自然，避免被当作自动化输入处理。

### 3. 轮次之间需要等待

Claude Code 回复完成后，TUI 需要时间重新渲染输入框。建议在检测到 `stop_reason == "end_turn"` 后等待 **2-3 秒**再发送下一条。

### 4. JSONL 读取必须处理不完整行

文件写入非原子——单条 JSONL 可能超过 `PIPE_BUF`（4096 bytes），`write()` 可能被拆分。读取时必须用行缓冲策略：只消费以 `\n` 结尾的完整行，保留尾部残行到下次拼接。否则 offset 越过残行后，该消息永久丢失。

### 5. 崩溃恢复：`--resume`

如果 PTY 进程异常退出，可以用 `--resume` 恢复 session：

```python
['claude', '--dangerously-skip-permissions', '--resume', session_id]
```

检测进程退出的方式：
- `proc.poll()` 返回非 None
- drain 线程收到 `OSError`（EIO 表示 slave 端已关闭）
- `_child_dead` 标志被设置

**结构化重试策略：**
1. 超时未收到 `end_turn` → 发送 ESC 中断
2. 等待 5 秒仍无响应 → `stop()` 杀进程
3. 用 `--resume` + 原 session_id 重新 `spawn()`

### 6. Session 文件位置计算

JSONL 文件路径规则：

```
~/.claude/projects/{project_hash}/{session_id}.jsonl
```

`project_hash` 是 cwd 的路径替换 `/` 为 `-`（保留开头的 `-`）：

```python
def get_jsonl_path(cwd: str, session_id: str) -> str:
    project_hash = cwd.replace('/', '-')  # 保留开头的 -
    # /home/ubuntu/my-project → -home-ubuntu-my-project
    return os.path.expanduser(
        f'~/.claude/projects/{project_hash}/{session_id}.jsonl'
    )
```

### 7. 子进程意外退出检测

不要只在 `stop()` 时才检查进程状态。应在 `wait_for_response` 循环中定期调用 `proc.poll()`，避免无限等待一个已经崩溃的进程。drain 线程中的 `EIO` 捕获也作为辅助信号。

### 8. Claude Code 启动时的 TUI 行为

启动时 Claude Code 会发送以下转义序列，无需处理但会产生需要 drain 的输出：

| 行为 | 转义序列 | 影响 |
|------|----------|------|
| 查询终端背景色 | `\x1b]11;?\x07` | 无响应时回退到暗色主题，无害 |
| 启用焦点报告 | `\x1b[?1004h` | 可能生成 `\x1b[I`/`\x1b[O` 输入序列 |
| 切换到备用屏幕 | `\x1b[?1049h` | TUI 输出在备用缓冲区 |
| 启用鼠标报告 | 各种 | 可能在输入流中产生额外转义序列 |
| 读取 CLAUDE.md | — | 增加启动时间，生成初始 JSONL 条目 |

## HTTP Stop Hook（推荐作为主检测方式）

HTTP Stop Hook 提供**零延迟的推送通知**，无 JSONL 轮询的延迟和 partial-write 问题。推荐作为回复完成的主检测方式，JSONL 轮询作为 fallback。

在 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "http",
            "url": "http://localhost:8000/api/hooks/stop",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Hook 的 POST body 包含：

```json
{
  "hook_event_name": "Stop",
  "session_id": "007c1594-...",
  "cwd": "/path/to/project",
  "stop_hook_active": true,
  "last_assistant_message": "回复的文本内容..."
}
```

`last_assistant_message` 字段直接提供最后一条 assistant 消息的文本，无需解析 JSONL。

### 双通道检测架构

```python
class ResponseDetector:
    """Stop Hook（主）+ JSONL 轮询（备）双通道检测"""

    def __init__(self, pty_inst: PTYInstance):
        self.pty_inst = pty_inst
        self.tracker = JsonlTracker(pty_inst.jsonl_path)
        self._hook_event = threading.Event()
        self._hook_data = None

    def on_stop_hook(self, data: dict):
        """HTTP Stop Hook 回调（由 web server 路由调用）"""
        if data.get('session_id') == self.pty_inst.session_id:
            self._hook_data = data
            self._hook_event.set()

    def wait_for_response(self, timeout: float = 120) -> dict | None:
        """等待回复完成，优先使用 Stop Hook，JSONL 作为 fallback"""
        # 尝试通过 Hook 快速检测
        if self._hook_event.wait(timeout=timeout):
            self._hook_event.clear()
            # Hook 只返回文本摘要，完整消息从 JSONL 获取
            time.sleep(0.5)
            for msg in self.tracker.read_new_messages():
                if (msg.get('type') == 'assistant' and
                        msg.get('message', {}).get('stop_reason') == 'end_turn'):
                    return msg
            return self._hook_data  # fallback 到 hook 数据

        # Hook 未触发（超时或未配置），回退到 JSONL 轮询
        return self.tracker.wait_for_response(self.pty_inst, timeout=5)
```

## 并发部署注意事项

运行多个 PTY 实例时需注意系统资源限制：

| 资源 | 默认限制 | 说明 |
|------|----------|------|
| PTY 数量 | `/proc/sys/kernel/pty/max` = 4096 | 每个实例占 1 对 PTY |
| 文件描述符 | `ulimit -n` = 1024（软限制） | 每实例约 5 个 fd，~200 实例耗尽 |
| 内存 | — | 每个 Claude Code（Node.js）约 100-300MB |
| CPU | — | Ink TUI 渲染意外地 CPU 密集 |
| API 限额 | 按 API key/组织 | 多实例共享，容易撞上 tokens-per-minute 限制 |

**扩容建议：**
- 提高 fd 限制：`resource.setrlimit(resource.RLIMIT_NOFILE, (8192, 8192))`
- 监控 PTY 使用量：`cat /proc/sys/kernel/pty/nr`
- 实例数根据可用内存规划，预留每实例 300MB

## 安全注意事项

### `--dangerously-skip-permissions` 的风险

此模式下 Claude Code 对文件系统有**不受限的读写执行权限**。已知风险案例：
- 递归删除文件（`rm -rf`）
- 通过 prompt injection 泄露数据（如隐藏在文档中的恶意指令）

### 推荐缓解措施

- **容器隔离**：在 Docker/devcontainer 中运行，配合 `--network none` 阻断网络
- **权限控制**：考虑使用 `auto` 模式（内置分类器审查每个操作）替代 `--dangerously-skip-permissions`
- **工具限制**：通过 `--allowedTools` 限制可用工具集
- **环境清洁**：只传递必要环境变量，不泄露 API key 等敏感信息
- **Session 文件权限**：JSONL 文件包含完整对话历史，确认为仅用户可读

## 与现有架构的集成思路

当前 `InstanceManager` 通过 `asyncio.create_subprocess_exec` 启动 `-p` 模式的 Claude Code。迁移到 PTY 模式的核心改动：

1. **替换进程创建方式**：`create_subprocess_exec` → `subprocess.Popen` + `pty.openpty()`
2. **替换输出消费方式**：stdout readline → HTTP Stop Hook（主）+ JSONL file watcher（备）
3. **替换消息发送方式**：每次创建新进程 → `send_prompt()` 写入已有 PTY
4. **简化 session 管理**：去掉 `--resume` 逻辑，上下文在同一进程内天然延续
5. **新增 PTY drain 线程**：独立线程持续清空 PTY 缓冲区防止进程阻塞
6. **新增健康检查**：定期 `proc.poll()` + drain 线程 EIO 检测，发现崩溃自动 `--resume` 恢复

JSONL 消息的 `message.content` 结构与 `-p --output-format stream-json` 的 assistant message 高度一致，现有的 `StreamParser` 和前端渲染逻辑可大量复用。

## 故障排查

| 故障 | 症状 | 检测方式 | 恢复策略 |
|------|------|----------|----------|
| PTY 缓冲区满 | 进程无输出，JSONL 停更 | JSONL mtime 长时间不变 | 检查 drain 线程是否存活 |
| 进程崩溃 | master fd 读取报 EIO | `proc.poll()` 返回非 None | `--resume` 重启 |
| API 限流 | 长时间无 `end_turn` | JSONL 中可能有系统消息 | 等待后自动恢复 |
| OOM Kill | 进程消失 | `proc.poll()` + `/proc/pid/` 不存在 | `--resume` 重启 |
| JSONL 损坏 | `json.JSONDecodeError` | 解析失败计数 | 行缓冲已处理；持续失败则检查磁盘 |
| 启动超时 | JSONL 文件未创建 | 文件在超时内不存在 | 杀进程重试 |
| 输入丢失 | JSONL 中无 user 消息 | 发送后 5 秒无 user 类型条目 | 增加轮次间等待，重试发送 |
| 工具执行卡死 | 长时间无 `end_turn` | 超时计时器 | 发送 ESC 中断，或杀进程 |

## 已验证的能力

以下功能通过实验验证可正常工作（Claude Code v2.1.141）：

- [x] PTY 启动交互模式
- [x] `skipDangerousModePermissionPrompt` 跳过 trust dialog
- [x] 简单文本回复
- [x] 多轮上下文延续（4+ 轮）
- [x] 工具调用（Bash、Read、Edit 等）
- [x] JSONL 实时写入和读取
- [x] `stop_reason == "end_turn"` 作为回复完成信号
- [x] ESC 键中断正在进行的操作
- [x] 中断后继续对话
- [x] Session 元数据：`kind=interactive`、`entrypoint=cli`

## 替代方案对比

| 方案 | 优势 | 劣势 | 适用场景 |
|------|------|------|----------|
| **PTY 交互模式**（本方案） | 完整交互模式、多轮复用、全功能 | 实现复杂、需要 drain 线程、时序敏感 | 需要长驻进程和完整功能的编排系统 |
| **`-p` + `--resume`** | 简单、结构化输出、无 PTY 复杂度 | 每轮启动开销、需管理 session ID | 简单的单轮或低频多轮场景 |
| **Agent SDK** | 官方支持、原生结构化 API、无 TUI | 独立的 credit pool（2026-06-15 起）、功能集可能不同 | 新项目、不需要 CLI 特有功能 |
| **pexpect** | 成熟的 PTY 库、模式匹配、超时处理 | 额外依赖、模式匹配对复杂 TUI 脆弱 | 快速原型验证 |
