# TODO

## High Priority

- [ ] Implement sentinel file mechanism for turn completion detection (learn from claude-pee)
- [ ] Add ANSI terminal capability response (DA1/DA2/DSR) for Ink framework compatibility
- [ ] Align Channel implementation with official Claude Code Channels API (research preview)
- [ ] Add `--bare` mode equivalent for isolated startup without auto-discovery

## Medium Priority

- [ ] Support structured output extraction via `--json-schema` in interactive mode
- [ ] Add session persistence/restore across process restarts (serialize SessionPool state)
- [ ] Implement health check endpoint for SessionPool monitoring
- [ ] Add configurable MCP server loading in PTYProcess (beyond pty-bridge)
- [ ] Write README.md with usage examples and API documentation

## Low Priority

- [ ] Add Windows support via ConPTY (currently Unix-only with pty.openpty)
- [ ] Benchmark PTY overhead vs official SDK subprocess approach
- [ ] Add optional pyte integration for terminal screen state tracking
- [ ] Implement session tagging and metadata for pool management
- [ ] Add metrics/observability (session count, latency, error rates)

## Done

- [x] Core PTYProcess with pty.openpty + drain loop
- [x] JsonlReader with partial-write safety and full event normalization
- [x] Session with async send_prompt and auto-resume
- [x] SessionPool with LRU eviction and concurrency control
- [x] BridgeHub for Channel message injection
- [x] ChannelServer MCP stdio implementation
- [x] Permission request relay (BridgeHub <-> ChannelServer)
- [x] Workspace trust dialog auto-confirm
- [x] PTY open-source research report
