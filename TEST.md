# 测试指南

## 快速单元测试

```bash
uv run pytest -q tests/test_pool.py tests/test_pool_reaper.py
```

`tests/test_pool.py` 必须覆盖 `Session.start` 在进程 spawn 后报错和调用方取消两条路径：未发布 Session 要先完成 shielded stop，才允许 `get_or_create` 重抛；重复取消也不能中断清理。

`tests/test_turn_alignment.py` 必须覆盖活跃 turn steer 的三种不确定边界：ACK 超时不终止仍在工作的 Claude 且禁止同 turn 重投、迟到 ACK 仍由原消费者接管，以及消费者丢失时仍回收精确进程。

## 全量测试

```bash
uv run pytest -q
```

`test_full_features.py`、`test_integration.py` 等用例会调用真实 Claude CLI，需要本机存在有效 Claude 登录。若返回 `401 OAuth access token has been revoked`，这是外部凭证基线失败；仍需单独运行所有不依赖真实账号的测试确认代码回归。
