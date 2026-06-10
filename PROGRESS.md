# PROGRESS — 经验教训沉淀

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
