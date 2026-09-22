# Changelog

## [1.5.1] - 2026-09-22

### Added

- `bin/skill-python`：统一选择 Skill 专属 Python，支持环境变量、Skill 内 `.venv`、专属 uv 环境和兼容回退。
- `bin/verify-skill`：使用同一解释器执行编译、测试和示例漂移检查。
- `bin/regenerate-examples`：不依赖系统 `python3` 的示例再生成入口。

### Changed

- README、安装脚本和安装完成提示不再建议直接调用 macOS 系统 `python3`。
- `bin/work-plan` 复用统一 Python 运行时选择器，避免多个入口的解析规则漂移。

### Fixed

- macOS 默认 Python 3.9 环境下，手工验证命令与安装脚本实际使用的 Python 3.12 不一致的问题。

## [1.5.0] - 2026-09-22

### Added

- 固定角色 Profile 的完整字段和配置 SHA-256 快照校验。
- Codex `config.toml` 并发上限读取与 `effective_capacity` 计算。
- fresh Worker 的 `fork_turns: "none"` 机械门禁，以及可由 hook 调用的 `guard-dispatch` 命令。
- `complete_task` 完整任务闭环和 `blocker_or_final` 低通信策略。
- actual dispatch、模型覆盖为空、写入路径和通信统计的执行证据。
- `doctor` 配置检查命令。
- macOS 原位替换脚本，保留目标 `.git` 和 `.venv`。
- GitHub Actions Python 3.10/3.12 CI 和示例漂移门禁。

### Changed

- WorkPlan schema 升级到 5。
- Execution record 升级到 6。
- Summary 和 Digest 升级到 3。
- 默认派发粒度从普通阶段动作改为可独立验收的完整任务闭环。
- Digest 增加 fresh/reuse、隔离派发、follow-up、中途消息、等待和状态轮询统计。
- 示例全部使用当前 schema 并可端到端验证。

### Fixed

- retry 现在验证 prior task 的 `worker_id` 确实属于被替换任务，避免 supersede 无关 Worker。
- 不再静默跳过字段不完整或非法的 Agent TOML。
- Planner 不再仅依赖调用方声明容量；显式配置时使用 Codex 并发上限。
- 实际写入路径越过计划所有权时拒绝 Summary。
