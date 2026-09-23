# WorkPlan 与 Audit Bundle 协议 1.6

WorkPlan 将多子代理编排中可机械验证的部分交给本地确定性工具：固定角色 Profile、配置容量、依赖、文件所有权、读写冲突、波次、隔离派发、Worker 身份、复用、fresh replacement、独立复核、执行证据、通信统计和会话级摘要。

Planner 不创建 Worker、不调用模型、不联网、不修改目标仓库。模型、推理档位和 sandbox 仍来自当前 Agent TOML；Planner 只读取并快照这些配置，不在派发时覆盖它们。

## 当前版本

```text
Planner                 1.5.1
WorkPlan schema         5
Execution record        6
Summary schema          3
Digest schema           3
Doctor report           2
Audit Bundle            1
```

## CLI

计划和执行审计：

```bash
./bin/work-plan \
  --agents-dir "$HOME/.codex/agents" \
  --codex-config "$HOME/.codex/config.toml" \
  plan DRAFT.json --output PLAN.json

./bin/work-plan validate PLAN.json
./bin/work-plan summary PLAN.json EXECUTION.json --json --output SUMMARY.json
./bin/work-plan digest --entry PLAN.json EXECUTION.json --json --output DIGEST.json
./bin/work-plan render-digest DIGEST.json --output DIGEST.md
./bin/work-plan guard-dispatch PLAN.json TASK_ID DISPATCH.json --audit AUDIT_ID_OR_PATH
./bin/work-plan doctor
```

持久化 Audit Bundle：

```bash
./bin/work-plan audit-init --repo-root "$PWD" --task-name "任务名称"
./bin/work-plan audit-import /tmp/existing-audit --repo-root "$PWD" --task-name "任务名称"
./bin/work-plan audit-stage AUDIT_ID_OR_PATH --name implementation
./bin/work-plan audit-finalize AUDIT_ID_OR_PATH
./bin/work-plan audit-verify AUDIT_ID_OR_PATH
./bin/work-plan audit-list
./bin/work-plan audit-show AUDIT_ID_OR_PATH
./bin/work-plan audit-delete AUDIT_ID_OR_PATH --yes
./bin/work-plan audit-prune
./bin/work-plan audit-prune --apply
```

全局 `--audit-root PATH` 可覆盖默认 `$MULTI_AGENT_AUDIT_ROOT` 或 `$CODEX_HOME/audits/multi-agent`。

返回码：`0` 表示成功，`2` 表示输入、配置或证据违反协议。

## 持久化 Audit Bundle

任何实际创建或复用 Worker 的任务都必须先创建 Bundle，并为每个 WorkPlan 阶段分配稳定路径。正式证据不得只保存在 `/tmp`。

### 默认位置

```text
${MULTI_AGENT_AUDIT_ROOT:-${CODEX_HOME:-$HOME/.codex}/audits/multi-agent}
└── <repo-name>-<repo-path-hash>/
    └── <UTC timestamp>-<task-slug>-<id>/
        ├── manifest.json
        ├── 01-implementation.draft.json
        ├── 01-implementation.plan.json
        ├── 01-implementation.dispatches/
        │   └── <task-id>.json
        ├── 01-implementation.execution.json
        ├── 01-implementation.summary.json
        ├── 01-implementation.summary.txt
        ├── SUBAGENT_EXECUTION_DIGEST.json
        └── SUBAGENT_EXECUTION_DIGEST.md
```

目录和文件在创建或关闭时分别收紧到 `0700` 和 `0600`。repo key 使用仓库目录名和绝对路径摘要，避免同名仓库冲突。

### Manifest version 1

```json
{
  "bundle_type": "MULTI_AGENT_AUDIT_BUNDLE",
  "bundle_version": 1,
  "audit_id": "20260922T013624Z-calc-add-fix-a1b2c3d4",
  "status": "closed",
  "created_at": "2026-09-22T01:36:24Z",
  "closed_at": "2026-09-22T01:40:00Z",
  "skill_release": "1.6.0",
  "planner_version": "1.5.1",
  "repository": {
    "root": "/private/tmp/mao-write-smoke",
    "name": "mao-write-smoke",
    "key": "mao-write-smoke-01234567"
  },
  "task": {
    "name": "修复 calc.add",
    "slug": "calc.add",
    "risk": "normal"
  },
  "retention": {
    "days": 14,
    "keep": false,
    "delete_after": "2026-10-06T01:36:24Z",
    "extended_for_attention": false
  },
  "stages": [],
  "artifacts": [],
  "summary": {},
  "verification": {
    "status": "passed",
    "verified_at": "2026-09-22T01:40:00Z",
    "errors": [],
    "warnings": []
  }
}
```

`artifacts` 为除 manifest 外每个文件记录：相对路径、类型、大小、SHA-256，以及适用的 `plan_id`、schema version 和 Planner version。关闭后的 Bundle 默认不可变；文件增加、缺失或字节变化都会使 `audit-verify` 失败。

### 生命周期

1. `audit-init` 创建 open Bundle，并打印路径；普通任务默认 14 天，高风险任务默认 90 天，`--keep` 永久保留。
2. `audit-stage` 分配单调递增的阶段前缀和所有标准路径；一个 dispatch 一个 JSON。
3. 主代理只把该任务的审计产物写入 Bundle。
4. 成功生成 Digest JSON 后运行 `audit-finalize`；它补生成 Digest Markdown 和 Summary text、校验关联、计算清单和哈希并关闭 Bundle。
5. `audit-verify` 使用 manifest 对关闭的 Bundle 做离线完整性校验，不读取当前 Agent TOML 或 `config.toml`。
6. `audit-list` 和 `audit-show` 提供可发现性；最终回复只提供 `audit_id`，不默认暴露绝对路径。
7. `audit-delete --yes` 只删除 closed、verified、无异常 Bundle；open、未验证或有异常 Bundle 需要 `--force`。
8. `audit-prune` 默认 dry-run，只选择 retention 已到期且安全删除的 Bundle；`--apply` 才实际删除。

若 Digest 含异常，`audit-finalize` 将普通短期 retention 自动延长到至少 90 天。`audit-prune` 默认跳过 attention Bundle。

### 完整性关联

Bundle 关闭前必须满足：

- draft、generated plan、execution 和 summary 的 `plan_id` 集合完全一致；
- Digest `plan_ids` 与 generated plan 集合一致；
- 每个 execution Worker 都有匹配的标准化 dispatch 文件；
- Digest 执行数、验收数和独立复核数与原始记录一致；
- Markdown Digest 等于 JSON Digest 的确定性 renderer 输出；
- 每个 audit-stage 分配的必需路径存在；
- 不存在 symlink；未知文件会记录 warning；
- manifest artifact inventory 与磁盘文件、大小和 SHA-256 一致。

### 迁移临时审计

```bash
./bin/work-plan audit-import /tmp/mao-write-smoke-audit \
  --repo-root /private/tmp/mao-write-smoke \
  --task-name "calc.add smoke test"
```

`audit-import` 复制普通文件、拒绝 symlink、忽略源 `manifest.json`，然后在持久化根目录生成新的 version 1 manifest 并关闭 Bundle。旧目录没有 `audit-stage` 信息时会产生 warning，但完整的 plan/execution/summary/digest 仍可验证和管理。

## Agent TOML 要求

每个角色必须在文档根显式声明：

```toml
name = "implementer"
description = "..."
model = "gpt-5.6-sol"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"
developer_instructions = """..."""
```

`description` 和 `developer_instructions` 是角色契约；`model` 与 `model_reasoning_effort` 是本 Skill 固定 Profile 和配置审计的额外要求。缺少、重复或非法角色文件会整体拒绝加载，不静默跳过。

Planner 使用完整 TOML 解析器，只读取顶层字段。Python 3.11+ 使用 `tomllib`；Python 3.10 使用 `tomli`。

## Codex 配置容量

Draft 的 `max_concurrent_workers` 是当前工作流请求上限。若 `config.toml` 显式配置：

```toml
[agents]
max_concurrent_threads_per_session = 4
```

则：

```text
effective_capacity = min(max_concurrent_workers, configured capacity)
```

Generated plan 记录 `codex_config_evidence`，包括配置文件名、SHA-256、配置上限和容量来源。这里的哈希只用于配置快照与漂移检测，不替代代码验证。

## Draft 根字段

```json
{
  "version": 5,
  "plan_id": "auth-cache-fix",
  "max_concurrent_workers": 3,
  "runtime_workers": [],
  "prior_tasks": [],
  "tasks": []
}
```

## 任务字段

```json
{
  "task_id": "auth-fix-a1",
  "task_name": "修复认证缓存失效",
  "agent_type": "debugger",
  "attempt": 1,
  "replaces_task_id": null,
  "depends_on": [],
  "read_paths": ["src/auth", "tests/auth"],
  "write_paths": ["src/auth/cache.py", "tests/auth/test_cache.py"],
  "independent_review": false,
  "review_of_task_ids": [],
  "reuse_worker_id": null,
  "accounting_scope": "not_available",
  "dispatch_method": "spawn_agent",
  "fork_turns": "none",
  "delivery_mode": "complete_task",
  "progress_policy": "blocker_or_final",
  "deliverable": "完成调查、修复、目标测试、必要修正和最终交付。",
  "acceptance_criteria": ["目标测试通过", "不改变无关合同"]
}
```

### 派发合同

Fresh Worker 必须使用：

```json
{
  "dispatch_method": "spawn_agent",
  "fork_turns": "none",
  "delivery_mode": "complete_task",
  "progress_policy": "blocker_or_final",
  "reuse_worker_id": null,
  "accounting_scope": "not_available"
}
```

复用 Worker 必须使用：

```json
{
  "dispatch_method": "followup_task",
  "fork_turns": null,
  "reuse_worker_id": "worker-existing",
  "accounting_scope": "current_attempt_delta",
  "delivery_mode": "complete_task",
  "progress_policy": "blocker_or_final"
}
```

WorkPlan 不包含模型覆盖字段；模型和 effort 只来自 Agent TOML。

### 路径规则

- 必须是仓库相对路径；
- 禁止绝对路径、Windows 盘符、`..` 和 glob；
- 写角色必须声明非空 `write_paths`；
- 只读角色不得声明 `write_paths`，且必须声明读取范围；
- 写入仓库根目录 `.` 会被拒绝；
- 同波次禁止写写和写读冲突；读读不冲突。

## Worker 身份

| 字段 | 用途 |
|---|---|
| `task_id` | 一次逻辑执行的唯一 ID；retry 必须更换 |
| `worker_id` | Planner 内部稳定机器标识，不含 `/` |
| `runtime_ref` | 运行时直接暴露的 opaque 引用，可为 `null` |
| `runtime_ref_source` | `unknown`、`spawn_metadata` 或 `thread_status_metadata` |

合法 provenance：

| `runtime_ref` | `runtime_ref_source` |
|---|---|
| `null` | `unknown` |
| 非空 opaque string | `spawn_metadata` |
| 非空 opaque string | `thread_status_metadata` |

显示名称、task name、nickname、Worker 自述、TOML 和本地配置都不是运行时身份来源。

## Runtime status、task outcome、retirement 与 supersession

四者不能混用：

- `status` / `final_status`：运行时生命周期；
- `task_outcome`：主代理依据验收条件作出的任务结果；
- `retired_from_followup`：运行时直接确认 stop、reclaim 或终态；
- `superseded_by_task_id`：编排层禁止旧 Worker 后续 follow-up 和复用。

`completed` 不证明任务 `accepted`。外部依赖只有 `prior_tasks.status: accepted` 才解除。

## Retry

- 每个逻辑任务最多两次 attempt；
- replacement 使用新 `task_id` 和 `attempt + 1`；
- `replaces_task_id` 必须指向 prior task；
- prior task 的 `worker_id` 必须实际属于被替换任务；
- 旧 Worker 必须非活跃；
- replacement 使用 fresh Worker，不得设置 `reuse_worker_id`；
- 已知非空 `runtime_ref` 不得与任何既有 Worker 重复；
- 旧 Worker 由 generated plan 标记 `superseded_by_task_id`；
- 第一次失败本身不触发更昂贵角色。

## Worker 复用

复用要求：

- Worker 状态为 `completed` 或 `accepted`；
- 角色相同；
- 未退休、未 supersede；
- 每个 WorkPlan 中同一 Worker 最多复用一次；
- 独立复核和 retry 不得复用；
- execution 中内部 `worker_id` 与已知 `runtime_ref` 必须一致。

## 独立复核

- 使用 `reviewer` 或 `critical_reviewer`；
- `independent_review: true`；
- `review_of_task_ids` 非空；
- 自动形成依赖；
- 使用 fresh Worker 和隔离上下文；
- 不复用实现 Worker 的线程。

## Generated plan

Planner 增加：

- `planner_version`；
- `effective_capacity`；
- `open_workers`、`available_slots`；
- `role_profiles`：实际使用角色的固定 Profile 快照；
- `codex_config_evidence`；
- `waves`、`ready_task_ids`、`deferred_task_ids`；
- `blocked_task_ids`、`blocked_reasons`；
- `superseded_worker_ids`；
- 每个可调度任务的 `assigned_wave`。

只派发 `ready_task_ids`。任何状态、配置、依赖或所有权变化后重新生成计划。

## 派发前门禁 `guard-dispatch`

`guard-dispatch` 接受 validated generated plan、ready task ID、标准化派发 JSON 和必填的持久化 Audit Bundle 引用。PLAN 与 DISPATCH 必须位于同一个已分配、仍处于 open 状态的 stage 中：

```bash
./bin/work-plan guard-dispatch \
  "$STAGE_PLAN" TASK_ID "$STAGE_DISPATCH" \
  --audit "$AUDIT_ID"
```

标准化派发 JSON：

```json
{
  "method": "spawn_agent",
  "task_name": "修复认证缓存失效",
  "agent_type": "debugger",
  "fork_turns": "none",
  "model": null,
  "reasoning_effort": null
}
```

它会重新验证计划和 Audit Bundle 成员关系，并拒绝：

- Bundle 已关闭、不存在或无效；
- plan / dispatch 不在持久化 Bundle 内；
- plan 与 dispatch 不属于同一个已分配 stage；
- 任务不在 `ready_task_ids`；
- method、task name 或 agent type 与计划不一致；
- fresh spawn 未显式使用 `fork_turns: "none"`；
- follow-up 错误携带 fork 策略；
- 任意非空模型或 reasoning effort 覆盖。

该命令不假设某一种 Codex Hook JSON envelope。主代理必须先把工具调用参数写入该 stage 的 dispatch 目录，再传入同一 Bundle 的 `--audit` 引用，以退出码 `0/2` 放行或拒绝。这样即使模型遵守派发合同，若计划或派发证据仍停留在临时目录，门禁也不会放行。

用户级 `subagent-spawn-policy-hook` 只做无状态的通用 spawn 参数检查，不调用此命令，
也不写入 Bundle。`guard-dispatch` 继续负责 ready_task、stage、audit 和 fresh/reuse 合同。
specialized tool path 可能绕过 Hook，因此执行后审计仍是必需步骤。

## Execution record version 6

```json
{
  "version": 6,
  "plan_id": "auth-cache-fix",
  "plan_command": ["./bin/work-plan", "plan", "draft.json"],
  "validate_command": ["./bin/work-plan", "validate", "plan.json"],
  "validate_status": "passed",
  "workers": [],
  "active_worker_ids_after_execution": [],
  "writes_observed": false,
  "communication": {
    "wait_call_count": 1,
    "wait_timeout_count": 0,
    "status_poll_count": 0
  }
}
```

每个实际执行 Worker 记录：

```json
{
  "task_id": "auth-fix-a1",
  "agent_type": "debugger",
  "worker_id": "worker-auth-fix",
  "runtime_ref": "/root/auth_fix",
  "runtime_ref_source": "spawn_metadata",
  "final_status": "completed",
  "task_outcome": "accepted",
  "active_after_close": false,
  "retired_from_followup": false,
  "retirement_source": "unknown",
  "observed_dispatch": {
    "method": "spawn_agent",
    "task_name": "修复认证缓存失效",
    "fork_turns": "none",
    "model_override": null,
    "reasoning_effort_override": null
  },
  "observed_write_paths": [
    "src/auth/cache.py",
    "tests/auth/test_cache.py"
  ],
  "parent_followup_count": 0,
  "worker_intermediate_message_count": 0
}
```

### 实际派发验证

- method、task name 和 `fork_turns` 必须与计划一致；
- fresh spawn 必须为 `fork_turns: "none"`；
- follow-up 的 `fork_turns` 必须为 `null`；
- `model_override` 和 `reasoning_effort_override` 必须为 `null`；
- fresh Worker 的内部和运行时身份不得复用既有 Worker；
- reused Worker 的身份必须与计划一致。

### 写入路径证据

`observed_write_paths`：

- 列表：已取得具体路径证据；每个路径必须位于计划所有权内；
- `[]`：确认未观察到写入；
- `null`：证据不足，Digest 产生异常。

批次级 `writes_observed` 必须与各 Worker 证据一致：

- 任一列表非空：`true`；
- 没有写入但存在 `null`：`null`；
- 全部为已知空列表：`false`。

## Summary 与 Digest

Summary 重新验证 generated plan 和 execution record，记录：

- Agent TOML 固定配置快照；
- 实际派发方式和隔离上下文；
- fresh/reuse 身份；
- runtime status 与 task outcome；
- 实际写入路径；
- follow-up、中途消息、等待、timeout 和状态轮询；
- 未执行 ready task、残留活跃 Worker 和异常证据。

Digest 按实际执行时间聚合多个 plan/execution 对，并输出紧凑用户可见概览。模型和 effort 是 Agent TOML 配置证据，不是运行时遥测。

同一批 entry 应生成：

- `SUBAGENT_EXECUTION_DIGEST.json`：重新验证 plan、execution、Agent TOML 和当前 `config.toml` 后形成的机器审计快照；
- `SUBAGENT_EXECUTION_DIGEST.md`：通过 `render-digest` 从上述 JSON 快照生成的用户可见输出。

```bash
./bin/work-plan render-digest \
  SUBAGENT_EXECUTION_DIGEST.json \
  --output SUBAGENT_EXECUTION_DIGEST.md
```

历史 JSON Digest 已经成功生成后，纯 Markdown 渲染不读取当前 Agent TOML 或 `config.toml`。当前配置发生变化时，重新运行 `digest` 会按设计触发配置漂移校验；不得为获得 Markdown 而修改旧计划证据或用当前配置重建历史计划。

最终回复必须直接复用 Markdown Digest，不得根据 JSON 手工重建表格。固定表头和分隔行为：

```markdown
| `agent_type` | 模型（Agent TOML） | 推理档位 | 执行尝试 | 验收通过 | 独立复核 |
|---|---|---|---:|---:|---:|
```

表头、分隔行和数据行必须保持六列；不得合并表头、改变列顺序、改写统计值或转义行首管道符。

## Doctor

`doctor` 静态检查：

- Agent TOML 字段完整、角色唯一、固定 Profile 有效；
- Codex 并发上限已显式配置；
- `multi_agent_v2` 已启用；
- spawn metadata 可见；
- 模型覆盖未暴露；
- 工具命名空间为 `agents`；
- `wait_agent` 和非代码模式已启用；
- wait timeout 为有效递增正整数。

Doctor 只能证明文件配置，不证明当前会话已经重新加载或运行时实际采用。
