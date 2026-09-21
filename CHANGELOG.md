# Changelog

## [1.4.2] - 2026-09-21

### Added

- 会话级 `SUBAGENT_EXECUTION_DIGEST`。
- 用户可见的紧凑子任务执行概览。
- Agent TOML 中模型和推理档位的配置证据展示。
- 独立的 `task_outcome` 任务验收状态。
- Python 3.10 `tomli` 条件依赖和完整 TOML 解析。
- fresh Worker 的 `runtime_ref` 身份隔离校验。

### Fixed

- 不再将 Worker `completed` 自动等同于任务验收成功。
- 未验收通过的依赖不会因运行时 `completed` 而错误解锁。
- fresh Worker 不得通过更换内部 `worker_id` 复用旧 `runtime_ref`。
- Python 3.10 与 Python 3.11+ 的 TOML 解析语义保持一致。

### Validation

- Python 3.12：46/46。
- Python 3.10.20 + tomli 2.3.1：46/46。
- fresh `critical_reviewer` 独立复核未发现发布阻断问题。
