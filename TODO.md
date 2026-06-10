# TODO

## High Priority（Phase 2 — CCM 接入）

- [ ] ChannelServer 增加 `ccm_done(summary)` tool：agent 显式上报任务完成（学 teleos_done），作为回合/任务完成的主信号
- [ ] CCM instance_manager 增加 PTY 模式（feature flag，保留 -p 回退）；message queue 消费者从"重启进程 + --resume"改为 inject 到活会话（开发环境 /home/ubuntu/Claude-Code-Manager-dev）
- [ ] cost 统计：交互模式无 result 事件，从 assistant usage 累加 token（Session 聚合后挂到完成事件上）
- [ ] `.mcp.json` 改写到临时目录 + `--mcp-config` 传入（避免污染用户项目）；若 channels 加载器只认项目级 .mcp.json，则 stop() 时清理

## Medium Priority（Phase 3 — 生命周期硬化）

- [ ] 崩溃分类（区分 auth 过期/binary 缺失=不重试 vs 运行期 crash=resume）+ stderr 环形缓冲诊断（学 Teleos crash-classifier）
- [ ] PTY 输出活动 → typing/idle 信号（drain loop 已记录 _last_output，暴露给 CCM）
- [ ] 超时升级策略：软超时 → Esc 中断 → 仍无完成再杀进程
- [ ] Add session persistence/restore across process restarts (serialize SessionPool state)
- [ ] Implement health check endpoint for SessionPool monitoring
- [ ] Write README.md with usage examples and API documentation
- [ ] Align Channel implementation with official Claude Code Channels API (research preview)

## Low Priority

- [ ] Add ANSI terminal capability response (DA1/DA2/DSR) for Ink framework compatibility
- [ ] Add `--bare` mode equivalent for isolated startup without auto-discovery
- [ ] Support structured output extraction via `--json-schema` in interactive mode
- [ ] Add configurable MCP server loading in PTYProcess (beyond pty-bridge)
- [ ] Add Windows support via ConPTY (currently Unix-only with pty.openpty)
- [ ] Benchmark PTY overhead vs official SDK subprocess approach
- [ ] Add optional pyte integration for terminal screen state tracking
- [ ] Implement session tagging and metadata for pool management
- [ ] Add metrics/observability (session count, latency, error rates)

## Done

- [x] **Phase 1: I/O 核心重构（2026-06-10）**
  - [x] 回合完成检测改用 `system/turn_duration` JSONL 哨兵（替代不可靠的 stop_reason=end_turn）
  - [x] jsonl_path 修正为 CC 实际规则（所有非字母数字 → `-`，spike 验证）
  - [x] spawn 前预写 .claude.json trust 条目 + enabledMcpjsonServers（Teleos 方案）
  - [x] 启动对话框通用自动应答（剥 ANSI + 折叠空白匹配 Entertoconfirm + 冷却 re-check）——修复了 dev-channels 确认框导致 channels 全坏的问题
  - [x] send_prompt 默认走 channel 注入（MCP notification），stdin 降级为 fallback
  - [x] stdin 路径改 bracketed-paste 整段写入（修 UTF-8 分裂/换行提前提交/长 prompt 慢）
  - [x] channels 端到端集成测试（test_integration.py::TestChannelInjectionIntegration）
- [x] Phase 0 spike：channel 注入唤起 idle 会话新 turn ✅ / 交互模式 JSONL 形态 / 路径规则 / trust 预写 ✅
- [x] Core PTYProcess with pty.openpty + drain loop
- [x] JsonlReader with partial-write safety and full event normalization
- [x] Session with async send_prompt and auto-resume
- [x] SessionPool with LRU eviction and concurrency control
- [x] BridgeHub for Channel message injection
- [x] ChannelServer MCP stdio implementation
- [x] Permission request relay (BridgeHub <-> ChannelServer)
- [x] Workspace trust dialog auto-confirm
- [x] PTY open-source research report
