# WorkPlan 协议

WorkPlan 用于把多子代理编排中可机械验证的部分交给本地确定性工具处理：依赖、文件所有权、读写冲突、波次、当前容量、fresh replacement、Worker supersession、Worker 复用、独立复核隔离，以及执行结果摘要。

Planner / Validator **不会**创建 Worker、调用模型、联网、修改仓库或选择模型。工程角色仍由 `SKILL.md` 选择；模型、推理档位、sandbox 和角色行为仍以当前生效的 Agent TOML 为准。审计工具只读取这些显式配置并将其标记为配置证据，不把它们伪装成运行时遥测。

当前执行链：

```text
用户请求
  → multi-agent-orchestration：是否委派、角色、边界和交付合同
  → work_plan.py：依赖、所有权、波次和容量
  → 当前 Agent TOML：模型、推理档位、sandbox 和角色行为
  → 主代理：派发、验收、整合和独立复核
  → work_plan.py summary：单计划完整机器审计
  → work_plan.py digest：当前用户任务的会话级聚合概览
```

当前没有 light / standard 角色变体，因此不设置 Cost Gate。若以后增加同角色的多个成本档，再单独设计，不应隐式覆盖现有 TOML。

所有可用于 WorkPlan 的角色 TOML 必须显式声明 `name`、`model`、`model_reasoning_effort` 和 `sandbox_mode`。缺少模型或推理档位时，Planner 拒绝加载该角色，避免用户可见概览把继承值或猜测写成确定配置。

角色配置通过完整 TOML parser 读取，并且只从解析结果的文档根读取上述字段。Python 3.11+ 使用标准库
`tomllib`；Python 3.10 需要安装本 Skill 的条件依赖：

```bash
python3 -m pip install -r requirements.txt
```

Python 3.10 的条件依赖固定在 `tomli>=2.0.1,<2.4`，以维持与 `tomllib` 一致的 TOML 1.0 解析口径。
不得使用逐行正则作为 TOML 回退解析器；正则无法可靠处理 table scope、合法行尾注释、转义和多行字符串。

## 何时必须生成 WorkPlan

以下任一情况必须先生成并校验 WorkPlan：

- 同一阶段准备创建两个或更多 Worker；
- 存在两个或更多写入任务；
- 任务之间存在依赖；
- 需要根据当前活跃 Worker 计算可用容量；
- 需要 retry；
- 需要复用 Completed Worker；
- 需要独立复核；
- 新任务可能与正在运行的 Worker 发生读写冲突。

单一、无依赖、无复用、无 retry、无并行冲突的 Worker 可以直接按 Skill 派发合同执行。

## 命令

从 Skill 目录调用：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  plan examples/work-plan.draft.json \
  --output /tmp/work-plan.json
```

派发前重新验证生成结果：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  validate /tmp/work-plan.json
```

执行后生成固定摘要：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  summary /tmp/work-plan.json /tmp/work-plan.execution.json \
  --output /tmp/work-plan.execution-summary.txt
```

需要结构化摘要时加 `--json`。

当前用户任务产生多个 WorkPlan 时，使用重复的 `--entry PLAN EXECUTION` 聚合：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  digest \
  --entry /tmp/plan-a.json /tmp/plan-a.execution.json \
  --entry /tmp/plan-b.json /tmp/plan-b.execution.json \
  --output /tmp/subagent-execution-digest.md
```

需要结构化聚合结果时加 `--json`。

返回码：

- `0`：计划、生成结果、执行摘要或聚合概览有效；
- `2`：输入、角色、依赖、所有权、生命周期、身份映射、执行证据或生成结果无效。

## 根字段

```json
{
  "version": 4,
  "plan_id": "auth-refactor",
  "max_concurrent_workers": 3,
  "runtime_workers": [],
  "prior_tasks": [],
  "tasks": []
}
```

## Worker 身份分层

三个标识不能混用：

| 字段 | 用途 | 约束 |
|---|---|---|
| `task_id` | WorkPlan 中一次逻辑执行的唯一任务 ID | retry 必须换新 ID |
| `worker_id` | Planner 内部稳定机器标识 | 只允许安全标识符，不含 `/` |
| `runtime_ref` | Codex 运行时独立暴露的 thread handle、agent reference 或其它原始身份引用 | opaque string，可含 `/`，不解析；未取得时为 `null` |
| `runtime_ref_source` | `runtime_ref` 的直接观测来源 | `unknown`、`spawn_metadata` 或 `thread_status_metadata` |

例如，运行时返回：

```text
/root/runtime_metadata_probe
```

正确表示为：

```json
{
  "worker_id": "worker-runtime-metadata-probe",
  "runtime_ref": "/root/runtime_metadata_probe",
  "runtime_ref_source": "spawn_metadata"
}
```

错误表示为：

```json
{
  "worker_id": "/root/runtime_metadata_probe"
}
```

### Runtime identity provenance gate

`runtime_ref` 和 `runtime_ref_source` 必须形成以下合法组合：

| `runtime_ref` | `runtime_ref_source` | 含义 |
|---|---|---|
| `null` | `unknown` | 当前 Surface 没有独立暴露运行时身份引用 |
| 非空 opaque string | `spawn_metadata` | 引用直接来自 Worker 创建响应中的独立身份元数据 |
| 非空 opaque string | `thread_status_metadata` | 引用直接来自线程状态、列表或查询接口中的独立身份元数据 |

以下组合会被 Planner / Summary renderer 拒绝：

- `runtime_ref: null` 配合非 `unknown` 来源；
- 非空 `runtime_ref` 配合 `unknown`；
- `runtime_ref` 使用字符串 `"unknown"` 而不是 JSON `null`；
- 来源填写 `task_name`、canonical display name、nickname、Worker 自述、TOML、config 或其它未批准值。

该 gate 强制显式来源分类与字段配对，但不能审计操作系统 shell history 或证明调用方没有伪造来源声明。
主代理仍必须只根据实际工具返回的独立身份字段填写记录；无法确认时使用 `null / unknown`。

`runtime_ref` 不是本地文件路径，也不是模型或权限证据。不得从中推断：

- `agent_type`；
- model；
- reasoning effort；
- sandbox；
- 任务是否成功。

主代理调用 follow-up、stop 或 reclaim 接口时，先用内部 `worker_id` 查找 runtime Worker，再把 `runtime_ref` 原样交给运行时。

## Runtime retirement 与编排 supersession

运行时状态、运行时是否确认 stop/reclaim，以及编排层是否还允许继续使用旧 Worker，是三个不同事实。

### 运行时 retirement evidence

`retired_from_followup` / `retirement_source` 只记录运行时直接证据：

```json
{
  "status": "completed",
  "retired_from_followup": true,
  "retirement_source": "reclaim_response"
}
```

合法组合：

| 运行时状态 | `retired_from_followup` | `retirement_source` | 含义 |
|---|---:|---|---|
| `pending` / `running` | `false` | `unknown` | Worker 仍活跃 |
| `completed` / `accepted` | `false` | `unknown` | 非活跃，运行时仍可能接受 follow-up |
| `completed` / `accepted` | `true` | `stop_response` / `reclaim_response` | 运行时接口直接确认 stop/reclaim |
| `failed` / `blocked` / `early_stopped` / `stopped` / `interrupted` / `reclaimed` | `true` | `runtime_terminal_status` 或直接 stop/reclaim 响应 | 运行时终态或已确认退休 |

以下会被拒绝：

- `pending` / `running` 却声明已退休；
- `retired_from_followup: true` 但来源为 `unknown`；
- `retired_from_followup: false` 却声明 stop/reclaim 来源；
- 仅凭完成通知、主代理意图、task name、nickname 或 Worker 自述伪造 retirement evidence；
- 将仍显示为 `completed` 的 Worker 改写为 `stopped` / `reclaimed`。

### 编排 supersession

当前 Codex Surface 可能让 `completed` Worker 继续接受 follow-up，但 fresh replacement 并不需要先取得
不存在的 stop/reclaim acknowledgement。只要旧 Worker 已非活跃，Planner 可以通过新计划建立编排边界：

```json
{
  "status": "completed",
  "retired_from_followup": false,
  "retirement_source": "unknown",
  "superseded_by_task_id": "pricing-explain-a2"
}
```

`superseded_by_task_id` 的含义是：

- 旧 Worker 的运行时状态仍按真实值记录；
- 旧 Worker 即使技术上仍可接受消息，主代理也不得再向其发送 follow-up；
- replacement 必须使用 fresh Worker、全新内部 `worker_id` 和 `reuse_worker_id: null`；
- 被 supersede 的 Worker 不得再用于 Completed Worker 复用；
- 同一旧 Worker 只能被一个 replacement supersede；
- `pending` / `running` Worker 不得被直接 supersede，必须先真实停止或等待其进入非活跃状态。

因此 replacement gate 要求旧 attempt Worker满足：

1. 实际状态不是 `pending` / `running`；
2. prior task 状态允许 replacement；
3. 新任务使用新 `task_id`、`attempt + 1` 和 fresh Worker；
4. 旧 Worker 尚未被另一个任务 supersede。

运行时 retirement evidence 仍可保留，但不再是已完成 Worker fresh replacement 的前置条件。

### `runtime_workers`

描述当前运行时已知 Worker。只有 `pending` / `running` 计入 `open_workers`；`completed`、`accepted` 和其它终态不占当前并发槽位。

```json
{
  "worker_id": "worker-auth-scan",
  "runtime_ref": "/root/auth_scan",
  "runtime_ref_source": "thread_status_metadata",
  "task_id": "auth-scan-a1",
  "agent_type": "analyst",
  "status": "completed",
  "retired_from_followup": false,
  "retirement_source": "unknown",
  "superseded_by_task_id": null,
  "read_paths": ["src/auth"],
  "write_paths": []
}
```

`runtime_ref_source` 为必填字段。`runtime_ref` 为 `null` 时来源必须为 `unknown`；非空引用只允许
`spawn_metadata` 或 `thread_status_metadata`。不同非空 `runtime_ref` 必须唯一。

支持状态：

```text
pending | running | completed | accepted | failed | blocked |
early_stopped | stopped | interrupted | reclaimed
```

活跃 Worker 必须提供当前读写所有权；Planner 会阻塞与其冲突的新任务。

### `prior_tasks`

仅用于 retry、已完成依赖和历史状态验证。`worker_id` 始终引用内部 ID，不填写 `runtime_ref`。

`prior_tasks.status` 是外部依赖的权威任务验收结果。只有 `accepted` 可以解除依赖；
`runtime_workers.status` 只表示 Worker 生命周期，不能覆盖 prior task 的 `rejected`、
`role_mismatch`、`failed`、`blocked` 等结果。仅观察到 `completed` / `accepted` 运行时状态、
但没有对应 `prior_tasks.status: accepted` 时，下游任务仍必须保持 blocked。

```json
{
  "task_id": "auth-fix-a1",
  "attempt": 1,
  "status": "failed",
  "worker_id": "worker-auth-fix-a1"
}
```

支持状态：

```text
accepted | failed | blocked | role_mismatch |
early_stopped | interrupted | rejected
```

## 任务字段

```json
{
  "task_id": "auth-fix-a2",
  "task_name": "修复认证缓存失效",
  "agent_type": "debugger",
  "attempt": 2,
  "replaces_task_id": "auth-fix-a1",
  "depends_on": ["auth-scan-a1"],
  "read_paths": ["src/auth", "tests/auth"],
  "write_paths": ["src/auth/cache.py", "tests/auth/test_cache.py"],
  "independent_review": false,
  "review_of_task_ids": [],
  "reuse_worker_id": null,
  "accounting_scope": "not_available",
  "deliverable": "修复根因并提供最小稳定回归测试。",
  "acceptance_criteria": [
    "失败用例在修改前可复现",
    "目标测试通过",
    "不改变无关认证合同"
  ]
}
```

### 路径规则

- 必须是仓库相对路径；
- 禁止绝对路径、Windows 盘符、`..` 和 glob；
- 写角色必须声明非空 `write_paths`；
- 只读角色不得声明 `write_paths`；
- 写入仓库根目录 `.` 不构成可靠所有权，会被拒绝；
- 同波次内禁止写写冲突和写读冲突；读读不冲突。

角色的只读/写入性质从当前 Agent TOML 的 `sandbox_mode` 读取，不在 WorkPlan 中复制一套角色表。

## Retry / fresh replacement

- 每个逻辑子任务最多 2 次 attempt；
- 首次执行必须为 `attempt: 1` 且没有 `replaces_task_id`；
- replacement 必须使用全新 `task_id`；
- `attempt` 必须等于被替换任务的 attempt + 1；
- prior task 必须是失败、阻塞、角色不匹配、early stop、中断或被拒绝状态；
- 旧 Worker 必须不是 `pending` / `running`；活跃 Worker 必须先真实停止或等待终止；
- 已非活跃的旧 Worker不要求 stop/reclaim acknowledgement；Planner 在 generated plan 中把其
  `superseded_by_task_id` 设置为新 `task_id`，并列入 `superseded_worker_ids`；
- replacement 必须使用 fresh Worker，`reuse_worker_id` 必须为 `null`；执行记录必须使用全新内部
  `worker_id`，且已观测到的非空 `runtime_ref` 不得等于计划中任何既有 Worker 的引用；
- 计划生成后不得再向被 supersede 的旧 Worker 发送 follow-up；
- 同一旧 Worker 不得被第二个 replacement 再次 supersede；
- `ROLE_MISMATCH` 后重新路由也属于第二次 attempt，不另开第三次机会。

`retired_from_followup` 仍只记录直接运行时 retirement evidence。不得为了通过 replacement gate 而伪造
`stop_response`、`reclaim_response` 或运行时终态。

## Worker 复用

设置 `reuse_worker_id` 时：

- Worker 必须存在；
- 状态必须为 `completed` 或 `accepted`，并且 `retired_from_followup` 必须为 `false`；
- `agent_type` 必须与新任务一致；
- `accounting_scope` 必须为 `current_attempt_delta`；
- 独立复核不得复用任何已有 Worker；
- retry 不得复用被停止的旧 attempt Worker；
- 实际运行时 follow-up 只能使用已验证 provenance 的 `runtime_ref`；未知时不得用 task name、nickname 或自述代替；
- 执行摘要中的 `worker_id` 与已知 `runtime_ref` 必须和计划中的复用 Worker 一致；
- 执行阶段可以用 `thread_status_metadata` 重新观察同一个已知引用，来源不必与最初的 `spawn_metadata` 相同。

`current_attempt_delta` 是统计口径声明。若当前运行时不提供可验证的增量数据，应把用量标记为不可用，不得把整个历史生命周期重新计入本轮。

## 独立复核

独立复核任务必须：

- 使用 `reviewer` 或 `critical_reviewer`；
- 设置 `independent_review: true`；
- 在 `review_of_task_ids` 中声明被复核任务；
- 使用 fresh Worker，不得设置 `reuse_worker_id`；执行记录必须使用全新内部 `worker_id`，且已观测到的
  非空 `runtime_ref` 不得等于计划中任何既有 Worker 的引用；
- 自动依赖被复核任务，因此不会与实现处于同一波次。

## 生成结果

Planner 增加：

- `open_workers`：当前 `pending` / `running` 数；
- `available_slots`：当前可用槽位；
- `waves`：满足依赖、容量和读写隔离的计划波次；
- `ready_task_ids`：当前可以创建的任务；
- `deferred_task_ids`：已规划但需要等待槽位或前序波次；
- `blocked_task_ids` / `blocked_reasons`：需要运行时变化或边界修正后重新规划的任务；
- `superseded_worker_ids`：本计划已在编排层禁止后续 follow-up / reuse 的旧 Worker；
- 每个可调度任务的 `assigned_wave`。

只派发 `ready_task_ids`。外部依赖只有在 `prior_tasks.status: accepted` 时才算已解决；Worker 的
`completed` 等运行时状态不能代替任务验收结果。槽位、活跃 Worker、依赖结果或文件所有权变化后，
使用最新状态重新运行 Planner；不要把旧计划当作持续有效的运行时事实。

## 执行记录

使用 WorkPlan 并实际派发 Worker 后，主代理保存一份执行记录：

```json
{
  "version": 5,
  "plan_id": "auth-refactor",
  "plan_command": [
    "./bin/work-plan",
    "--agents-dir",
    "/Users/sc/.codex/agents",
    "plan",
    "/tmp/work-plan.draft.json",
    "--output",
    "/tmp/work-plan.json"
  ],
  "validate_command": [
    "./bin/work-plan",
    "--agents-dir",
    "/Users/sc/.codex/agents",
    "validate",
    "/tmp/work-plan.json"
  ],
  "validate_status": "passed",
  "workers": [
    {
      "task_id": "auth-scan-a1",
      "agent_type": "analyst",
      "worker_id": "worker-auth-scan",
      "runtime_ref": "/root/auth_scan",
      "runtime_ref_source": "thread_status_metadata",
      "final_status": "completed",
      "task_outcome": "accepted",
      "active_after_close": false,
      "retired_from_followup": true,
      "retirement_source": "reclaim_response"
    }
  ],
  "active_worker_ids_after_execution": [],
  "writes_observed": false
}
```

### 执行记录规则

- `plan_id` 必须与生成计划一致；
- `validate_status` 固定为 `passed`；验证失败不得派发 Worker；
- `plan_command` / `validate_command` 是主代理报告的实际参数数组；renderer 验证结构，但无法审计 shell history；
- 每个执行任务必须来自生成计划的 `ready_task_ids`；
- `agent_type` 必须与计划一致；
- fresh 任务必须使用新的内部 `worker_id`；若执行记录提供非空 `runtime_ref`，该引用不得匹配
  generated plan 中任何既有 runtime Worker；
- reuse 任务必须使用被复用 Worker 的内部 `worker_id`，且已知非空 `runtime_ref` 必须一致；
- 每个 Worker 必须同时提供 `runtime_ref_source`；`null / unknown` 或非空引用加批准来源是唯一合法组合；
- `runtime_ref` 未暴露时写 `null`，来源写 `unknown`；不得根据 TOML、任务名、canonical display name、nickname 或 Worker 自述推断；
- `final_status` 只记录运行时状态，并与 `active_after_close` 保持一致：只有 `pending` / `running` 是活跃状态；
- `task_outcome` 记录主代理依据派发合同和验收条件作出的任务结果判断，不从 `final_status` 推导；允许值为 `accepted`、`failed`、`blocked`、`role_mismatch`、`early_stopped`、`interrupted`、`rejected` 和 `not_evaluated`；
- 运行中的 Worker 只能使用 `task_outcome: not_evaluated`；运行时 `completed` 仍可能对应 `role_mismatch`、`rejected` 或其它未验收通过结果；
- 主代理只有在实际检查交付物与验收条件后才能填写 `accepted`；renderer 验证字段取值与状态组合，但不能独立证明验收判断本身正确；
- 每个 Worker 必须同时提供 `retired_from_followup` 和 `retirement_source`；运行时状态与运行时 retirement 分开记录；
- generated plan 中的 `superseded_worker_ids` 是编排层门禁，不代表运行时状态或 retirement evidence；
- `completed` / `accepted` 可以配合 `false / unknown` 保持可复用，也可以在直接 stop/reclaim 响应后配合
  `true / stop_response|reclaim_response` 退出 follow-up；不得把状态伪改成 stopped/reclaimed；
- `active_worker_ids_after_execution` 列出执行结束后所有仍处于 Pending/Running 的内部 Worker ID；
- `writes_observed` 为三态：`true`、`false`、`null`。`null` 表示证据不足，不得当作无写入。

## 完整机器审计 `WORKPLAN_EXECUTION_SUMMARY`

`summary` 命令会：

1. 重新验证 generated WorkPlan；
2. 验证执行记录与 `ready_task_ids`、角色、fresh/reuse 身份、runtime identity provenance 和 supersession 集合的一致性；fresh 任务不得使用任何计划已知的非空 `runtime_ref`；
3. 分别验证 Worker 运行时 `final_status` 和主代理验收 `task_outcome`，不把两者互相推导；
4. 根据执行任务的 `agent_type` 读取对应 Agent TOML，并记录配置模型、推理档位和 sandbox；
5. 生成固定顺序、固定字段的完整机器审计；
6. 显式列出未执行的 ready task；
7. 同时显示 `runtime_ref` 与 `runtime_ref_source`；未暴露引用时二者分别显示为 `unknown` / `unknown`；
8. 将证据不足的写入状态显示为 `unknown`。

完整摘要中的角色配置字段：

| 字段 | 含义 |
|---|---|
| `configured_model` | 对应 Agent TOML 显式声明的模型 |
| `configured_model_reasoning_effort` | 对应 Agent TOML 显式声明的推理档位 |
| `configured_sandbox_mode` | 对应 Agent TOML 显式声明的 sandbox |
| `profile_source` | 当前固定为 `agent_toml` |
| `profile_file` | 提供该角色配置的 TOML 文件名 |

这些值证明的是摘要生成时读取到的角色配置。它们不证明运行时接口单独回报了实际模型和推理档位，也不证明修改后的文件已经被既有 Codex 会话重新加载。

固定摘要示例：

```text
WORKPLAN_EXECUTION_SUMMARY
summary_version: 2
planner_version: 1.4.2
plan_command: ["./bin/work-plan", "--agents-dir", "/Users/sc/.codex/agents", "plan", "/tmp/draft.json", "--output", "/tmp/plan.json"]
validate_command: ["./bin/work-plan", "--agents-dir", "/Users/sc/.codex/agents", "validate", "/tmp/plan.json"]
plan_id: auth-refactor
validate_status: passed
effective_capacity: 2
open_workers_at_plan_time: 0
available_slots_at_plan_time: 2
waves:
  - wave: 1
    task_ids: ["auth-scan-a1"]
ready_task_ids: ["auth-scan-a1"]
executed_task_ids: ["auth-scan-a1"]
not_executed_ready_task_ids: []
superseded_worker_ids: []
workers:
  - task_id: auth-scan-a1
    task_name: "检查认证缓存"
    agent_type: analyst
    independent_review: false
    configured_model: "configured-model"
    configured_model_reasoning_effort: "high"
    configured_sandbox_mode: read-only
    profile_source: agent_toml
    profile_file: "analyst.toml"
    worker_id: worker-auth-scan
    runtime_ref: "/root/auth_scan"
    runtime_ref_source: thread_status_metadata
    final_status: completed
    task_outcome: accepted
    active_after_close: false
    retired_from_followup: true
    retirement_source: reclaim_response
active_workers_after_execution: 0
active_worker_ids_after_execution: []
writes_observed: false
```

完整摘要是机器审计产物，不默认逐字进入最终用户回复。

## 会话级 `SUBAGENT_EXECUTION_DIGEST`

`digest` 接受一个或多个 WorkPlan / execution 对。`--entry` 必须按实际执行时间顺序提供；工具对每一对重新执行与 `summary` 相同的校验，再按当前用户任务聚合：

- 唯一 `plan_id` 和唯一已执行 `task_id`；
- 各 `agent_type` 的配置模型、推理档位、sandbox、执行尝试数、验收通过数、独立复核数、运行时终态分布和任务验收结果分布；
- 计划校验数、总执行尝试数、验收通过数和独立复核数；
- 未执行 ready task、残留活跃 Worker、superseded Worker 去重集合；
- 每个执行批次的写入证据 true / false / unknown 计数；
- 未验收通过、尚未验收、未执行任务、残留活跃 Worker 和未知写入证据组成的异常列表。

默认 Markdown 结果示例：

```markdown
### 子任务执行概览

| `agent_type` | 模型（Agent TOML） | 推理档位 | 执行尝试 | 验收通过 | 独立复核 |
|---|---|---|---:|---:|---:|
| `critical_reviewer` | `configured-model` | `high` | 2 | 2 | 2 |

计划校验：2/2 通过；执行尝试：2/2 验收通过；独立复核：2；未执行 ready task：0；残留活跃 Worker：0；写入观测：0 有写入、2 无写入、0 未知。

**异常：** 无。

模型和推理档位来自对应 Agent TOML 的显式配置，属于配置证据，不表示运行时接口已单独回报并验证这些值。

验收通过仅按执行记录中的 `task_outcome=accepted` 统计；`final_status` 只表示 Worker 运行时状态，二者不得互相推导。
```

用户可见回复如何组织这份概览由当前会话的 `developer_instructions` 决定。正常情况下只展示聚合结果，不展示 `plan_command`、`validate_command`、绝对路径、`runtime_ref`、完整 Worker ID、完整波次、空数组或重复的正常布尔字段，也不使用 `<details>` 包裹原始机器摘要。用户明确要求原始审计或聚合结果存在异常时，再提供相应详细记录。
