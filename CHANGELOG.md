# Changelog

## [1.6.0] - 2026-09-22

### Added

- `guard-dispatch --audit` 强制 plan 与 dispatch 在派发前已写入同一 open Audit Bundle stage，避免合规调用仍只留下临时证据。
- 持久化 Audit Bundle version 1，默认存储于 `$CODEX_HOME/audits/multi-agent`。
- `audit-init`：创建版本化、私有权限、带 retention 的任务审计目录。
- `audit-stage`：为每个 WorkPlan 阶段分配稳定的 draft、plan、dispatch、execution 和 summary 路径。
- `audit-finalize` / `audit-verify`：关闭 Bundle、生成可读产物、记录 artifact 清单和 SHA-256，并做离线完整性校验。
- `audit-list` / `audit-show`：按 `audit_id` 发现和查看历史审计。
- `audit-delete`：带显式确认和异常保护的安全删除。
- `audit-prune`：按 retention dry-run 或批量清理。
- `audit-import`：迁移既有 `/tmp` 审计目录。
- Audit Bundle 生命周期、manifest 和保留策略的单元及 CLI 端到端测试。

### Changed

- 任意实际子代理派发现在都要求 WorkPlan 和持久化 Audit Bundle，包括单一无依赖 Worker。
- 普通任务默认保留 14 天，高风险任务默认 90 天；异常 Bundle 至少保留 90 天。
- 最终回复提供 `audit_id`，通过 `audit-list` / `audit-show` 定位，不默认暴露绝对路径。
- macOS 安装脚本初始化私有 Audit Root。

### Fixed

- 子代理产物散落在 `/tmp` 或任意路径，后续无法发现、版本审计或安全删除的问题。

## [1.5.3] - 2026-09-22

### Added

- `render-digest`：从已验证的 `SUBAGENT_EXECUTION_DIGEST.json` 纯渲染 Markdown，不重新读取当前 Agent TOML 或 `config.toml`。
- 历史 Digest 配置漂移回归测试，覆盖计划生成后 `config.toml` 字节变化的场景。

### Changed

- 用户可见 Markdown 现在从已保存的 JSON Digest 快照生成；`digest` 继续负责重新校验 plan/execution 和当前配置。

### Fixed

- 当前 `config.toml` 与执行时配置哈希不一致时，仅为补生成 Markdown 而重新运行 `digest` 会触发 `codex_config_evidence` 漂移错误的问题。

## [1.5.2] - 2026-09-22

### Added

- Digest Markdown 表格契约回归测试，固定表头、分隔行和每列顺序。

### Changed

- 使用 WorkPlan 的任务同时生成 JSON Digest 和 Markdown Digest。
- 最终回复中的“子任务执行概览”必须直接复用本地 renderer 输出，不得根据 JSON 手工重建表格。

### Fixed

- 防止最终回复在手工拼接时合并 `agent_type`、模型、推理档位、执行尝试、验收通过和独立复核表头。

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
