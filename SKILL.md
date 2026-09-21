---
name: multi-agent-orchestration
description: 为适合独立并行、上下文隔离或独立复核的 Codex 任务选择子代理，分配文件所有权、必要上下文和交付合同。用于用户明确要求委派，或全局、项目指令已授权且委派具有实际收益时；小型、紧耦合或立即阻塞的工作由主代理直接完成。
---

# 子代理编排

委派用于推进真正独立的工作支线、隔离大量外围上下文，或取得必要的独立复核。
委派不得扩大用户授权、任务范围、文件所有权或外部写入权限。

模型、推理档位、权限意图和角色专属行为由对应角色 TOML 管理。本 Skill 只维护稳定的
职责边界、路由顺序、派发合同和整合流程。

执行层级固定为：

```text
用户请求
  → 本 Skill：是否委派、工程角色、职责边界、sandbox 意图和交付合同
  → 轻量 WorkPlan Planner / Validator：依赖、读写冲突、波次和当前容量
  → 角色 TOML：固定模型、固定推理档位、sandbox 和角色行为
  → 主代理：派发、验收、整合与独立复核
```

当前角色映射不设置 light / standard 变体，也不使用 Cost Gate 改写角色 TOML。质量与模型配置
以当前角色文件为准；Planner 只校验编排，不降低模型或 reasoning effort。

## 是否委派

只有至少满足一个条件时才委派：

- 存在可以独立推进、不会立即阻塞主线程的工作支线；
- 存在所有权不重叠、可以安全并行的实现工作；
- 大量外围调查会显著污染主线程上下文；
- 风险边界要求独立只读复核；
- 用户明确要求使用子代理。

以下情况由主代理直接完成：

- 工作量小且与当前修改紧密耦合；
- 子任务不能定义独立交付物；
- 调查结果会立即决定下一行修改；
- 文件或状态所有权无法可靠分离；
- 委派和整合成本接近或高于直接完成成本。

一个请求包含多种不同交付物时，先拆成独立子任务，再分别路由。不要为了减少代理数量，
强行让一个角色同时承担调查、设计、实现和复核。

委派后，主代理继续处理无依赖且不重叠的关键路径，不在可以推进时空等子代理。

## 路由判定

按以下顺序判断。命中明确条件后停止，不因为模型偏好、文件数量或任务篇幅改选更复杂角色。

### 1. 已有候选实现或实际 diff，任务目标是复核

候选实现包括工作区修改、提交、补丁、PR diff 或已经落地的迁移方案。
问题描述、计划和尚未实现的设计不属于候选实现。

- 变更触及公开合同、持久化数据、权限、并发、迁移、兼容性或发布门禁：
  使用 `critical_reviewer`。
- 其他普通正确性、回归风险和验证缺口复核：
  使用 `reviewer`。

### 2. 没有候选实现，任务是只读工作

- 需要决定未来系统边界、状态归属、依赖方向或难以逆转的技术方案：
  使用 `architect`。
- 需要调查现有系统中的并发交错、数据一致性、复杂生命周期、遗漏状态、
  部分失败或故障传播：
  使用 `deep_auditor`。
- 需要解释现有系统的跨模块根因、性能瓶颈、非平凡合同或变更影响：
  使用 `analyst`。
- 只需要定位文件、符号、入口、配置、调用关系、测试入口或归纳日志：
  使用 `default`。

时间方向是重要边界：

- “当前为什么这样运行”通常属于 `analyst`；
- “当前复杂状态是否存在反例”属于 `deep_auditor`；
- “未来应该如何组织”属于 `architect`。

### 3. 任务需要写入

先判断是否已经存在可观察故障。

#### 已有故障证据

故障证据包括报错、失败测试、日志、监控异常、复现步骤或明确错误行为。

- 修复需要同时维持多个相互制约的不变量：
  使用 `deep_engineer`。
- 其他常规根因定位和代码修复：
  使用 `debugger`。

#### 没有故障证据

- 修改规则已经完整给定，不需要产品、接口、数据或失败语义判断：
  使用 `mechanical`。
- 工作属于单一且清晰输入输出合同的简单脚本、数据处理、批量操作或小型独立功能：
  使用 `utility`。
- 实现需要同时维持多个相互制约的不变量：
  使用 `deep_engineer`。
- 其他验收行为和文件所有权明确的常规应用功能：
  使用 `implementer`。

### 4. 深度角色的触发条件

使用 `deep_engineer`、`deep_auditor` 或 `critical_reviewer`，应有具体机制支持。
常见机制包括：

- 跨模块状态所有权；
- 事务或原子性；
- 并发、竞态或执行顺序；
- 持久化格式和数据完整性；
- 在线迁移、回滚或跨版本兼容；
- 权限、认证或信任边界；
- 部分失败、补偿和恢复；
- 生命周期状态机；
- 多个公共合同必须同时保持。

文件多、仓库大、任务描述长、预计耗时长、第一次尝试失败或模型更昂贵，
都不能单独触发深度角色。

当两个角色都可能适用时，选择职责更窄、复杂度更低的角色；只有存在其无法覆盖的具体
合同、状态或风险机制时才升级。

没有明确匹配角色，或独立委派没有实际收益时，由主代理直接完成。

## 运行时和角色选择

- 只使用当前运行时实际暴露的角色、工具名称和参数，不根据旧文档猜测 schema。
- 新建子代理时显式指定 `agent_type`。省略 `agent_type` 仅用于有意使用自定义
  `default` 角色进行只读事实探查，不把省略角色作为通用执行回退。
- 角色 TOML 中固定的模型和推理档位视为锁定配置。派发时不传冲突的模型或
  reasoning effort 覆盖。
- 选择已配置角色即接受该角色预设的模型和推理档位，不因正常使用 Astra 或较高
  reasoning effort 再次请求确认。
- 不因普通角色第一次失败就升级到更昂贵的角色。只有新证据表明任务实际满足更复杂角色的
  前置条件时才重新路由。
- 运行时支持任务名时，为每个子任务提供唯一、简短且能表达交付物的 `task_name`。
- WorkPlan 的 `worker_id` 是 Planner 内部稳定机器标识，只允许安全标识符；运行时独立暴露的 thread handle、
  agent reference 或类似 `/root/runtime_metadata_probe` 的 opaque 引用放入独立的 `runtime_ref`。
- 每个 `runtime_ref` 必须同时声明 `runtime_ref_source`：未取得引用时使用 `runtime_ref: null` +
  `runtime_ref_source: unknown`；只有直接来自创建响应的独立身份元数据才能使用 `spawn_metadata`，只有直接来自
  线程状态或列表接口的独立身份元数据才能使用 `thread_status_metadata`。
- `task_name`、canonical display name、nickname、Worker 正文或自述、TOML 和配置文件都不是运行时身份来源。
  只观察到这些字段时必须保持 `null / unknown`，不得猜测或把显示标签改名后充当 `runtime_ref`。
- `runtime_ref` 是 opaque value，不当作本地路径，也不从中推断 agent type、model、reasoning、sandbox 或状态。
  主代理调用 follow-up、stop 或 reclaim 接口时，先用内部 `worker_id` 找到对应 runtime Worker，再把已验证来源的
  `runtime_ref` 原样交给运行时；不得把 `runtime_ref` 复制到 `worker_id`。
- 运行时 `status` 只记录实际观察到的状态；不得因为调用过 stop/reclaim 就把仍显示为 `completed` 的 Worker
  改写为 `stopped` 或 `reclaimed`。是否允许继续 follow-up 使用独立字段 `retired_from_followup`。
- `retired_from_followup: true` 必须同时提供 `retirement_source`：直接 stop 响应用 `stop_response`，
  直接 reclaim 响应用 `reclaim_response`，运行时明确显示 failed/blocked/early_stopped/stopped/interrupted/reclaimed
  等终态时用 `runtime_terminal_status`。仅有完成通知、主代理意图、task name、nickname 或 Worker 自述不构成 retirement evidence。
- `retired_from_followup` 只记录运行时是否直接确认 stop/reclaim/终态，不再作为 fresh replacement 的前置条件。
- `completed` / `accepted` 且 `retired_from_followup: false` 的 Worker 可以继续具备运行时 follow-up 能力；是否允许编排层继续使用它，由独立的 `superseded_by_task_id` 决定。
- fresh replacement 计划生成后，旧 Worker 的 `superseded_by_task_id` 指向新 `task_id`。这表示旧 Worker 在编排层不得再收到 follow-up 或被复用，即使运行时技术上仍接受消息；不得因此伪造运行时 `status` 或 retirement evidence。
- 运行时支持历史继承控制时，默认不继承历史或只继承必要的最近上下文。能够准确重述时，
  只发送完成任务所需的背景；只有无法可靠压缩既有决策时才继承完整历史。
- 相关子任务只有在同一工作流、角色一致、旧上下文仍有价值、Worker 当前不在运行、
  文件所有权不冲突且不要求独立复核时，才优先复用现有代理。
- 复用 Worker 时必须给出新的 `task_id`、当前交付物和验收条件；若运行时提供用量数据，
  只统计本轮新增区间，不重新计算历史生命周期累计。
- 独立复核必须使用 fresh `reviewer` 或 `critical_reviewer`，不得复用原实现 Worker。
- 文件变化不证明当前会话已重载角色或配置；需要依赖新配置时使用新会话或检查实际状态。

## WorkPlan 与机器校验

WorkPlan 将可机械验证的编排约束交给本地确定性工具。Planner 不创建 Worker、不调用模型、
不联网、不修改仓库，也不选择或覆盖模型。当前 draft / generated WorkPlan 使用 `version: 4`。

以下任一情况必须先生成并校验 WorkPlan：

- 同一阶段准备创建两个或更多 Worker；
- 存在两个或更多写入任务；
- 任务之间存在依赖；
- 需要按当前活跃 Worker 计算容量；
- 需要 retry、复用 Completed Worker 或执行独立复核；
- 新任务可能与正在运行的 Worker 发生读写冲突。

单一、无依赖、无复用、无 retry、无并行冲突的 Worker 可以直接按派发合同执行。

从 Skill 目录运行：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  plan /path/to/work-plan.draft.json \
  --output /tmp/work-plan.json

./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  validate /tmp/work-plan.json
```

WorkPlan 至少声明：

- 唯一 `task_id`、简短 `task_name`、`agent_type`、独立交付物和验收条件；
- `depends_on`；
- 精确的仓库相对 `read_paths` / `write_paths`；
- `attempt` 与可选 `replaces_task_id`；
- 是否为 `independent_review` 及其 `review_of_task_ids`；
- 可选 `reuse_worker_id` 和 `accounting_scope`；
- 当前 runtime Worker 的内部 `worker_id`、opaque `runtime_ref`、必填 `runtime_ref_source`、逻辑 `task_id`、角色、实际运行时状态、`retired_from_followup`、`retirement_source`、可选 `superseded_by_task_id` 和读写所有权。

Planner / Validator 必须验证：

角色 TOML 必须由符合 TOML 语法的解析器完整解析后再读取顶层字段。Python 3.11+ 使用标准库
`tomllib`；Python 3.10 使用 `requirements.txt` 声明的条件依赖 `tomli`。不得使用逐行正则回退，
因为它无法可靠区分顶层字段与 `[table]` 内同名字段，也无法正确处理合法注释、转义和多行字符串。

1. 角色存在，且实际 Agent TOML 的 `sandbox_mode` 与读写声明一致；
2. 路径为精确仓库相对路径，不含绝对路径、`..` 或 glob；
3. 依赖存在且无环；独立复核隐式依赖被复核任务；外部依赖只有明确的
   `prior_tasks.status: accepted` 才算已解决，Worker 的 `completed` 等运行时状态不得覆盖或代替任务验收结果；
4. 同波次无写写或写读冲突；读读不冲突；
5. `pending` / `running` 才计入当前并发，历史 `completed` 不扣槽位；
6. 与活跃 Worker 所有权冲突的任务保持 blocked，状态变化后重新规划；
7. 只有生成结果中的 `ready_task_ids` 可以立即派发；
8. 运行时状态、依赖结果或所有权发生变化后重新生成计划，不沿用失效波次；
9. `worker_id` 与 `runtime_ref` 分离，复用时内部身份和原始运行时引用均保持一致；fresh 任务必须使用
   新内部 `worker_id`，且已观测到的非空 `runtime_ref` 不得匹配计划中的任何既有 Worker；
10. `runtime_ref` 与 `runtime_ref_source` 必须构成合法 provenance pair，显示标签、自述和配置来源会被拒绝；
11. 运行时 `status`、运行时 retirement 和编排层 supersession 分离：replacement 只要求旧 Worker 已非活跃；Planner 将旧 Worker 标记为 `superseded_by_task_id=<new task_id>`，强制 fresh Worker 并禁止后续复用。只有 Pending/Running 旧 Worker 仍要求先真实停止或等待终止。

完整字段、状态和示例见 `references/work-plan.md`。

### 执行审计与用户可见概览

凡使用 WorkPlan 并实际派发了 Worker，完成当前计划执行后必须保存执行记录，并生成一份完整的
`WORKPLAN_EXECUTION_SUMMARY` 机器审计产物：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  summary /tmp/work-plan.json /tmp/work-plan.execution.json \
  --json \
  --output /tmp/work-plan.execution-summary.json
```

完整执行摘要用于机械校验、故障排查和原始证据追溯，不默认逐字放入用户可见回复。当前 execution
record 使用 `version: 5`，并至少声明：

- 实际使用的 `plan_command` 和 `validate_command` 参数数组；
- 与计划一致的 `plan_id` 和 `validate_status: passed`；
- 每个实际派发 Worker 的 `task_id`、`agent_type`、内部 `worker_id`、`runtime_ref`、
  `runtime_ref_source`、`final_status`、`task_outcome`、`active_after_close`、`retired_from_followup` 和 `retirement_source`；
- 执行结束后所有仍处于 Pending/Running 的内部 Worker ID；
- `writes_observed`：观察到写入为 `true`，确认未观察到写入为 `false`，证据不足为 `null`。

`final_status` 只记录 Worker 的运行时状态；`task_outcome` 记录主代理依据派发合同和验收条件作出的任务结果判断。
允许值为 `accepted`、`failed`、`blocked`、`role_mismatch`、`early_stopped`、`interrupted`、`rejected` 和
`not_evaluated`。不得因为 Worker 运行时显示 `completed` 就自动填写 `accepted`。运行中的 Worker 只能使用
`task_outcome: not_evaluated`。主代理只有在实际检查交付物与验收条件后才能填写 `accepted`；renderer 只能验证
字段取值和状态组合，不能独立证明验收判断本身正确。

Summary renderer 会重新验证生成计划，只接受 `ready_task_ids` 中的实际执行任务，并检查 fresh/reuse
身份、runtime identity provenance、retirement evidence、superseded Worker 集合、final status 与活跃状态一致性。
fresh 任务除使用新内部 `worker_id` 外，任何已观测到的非空 `runtime_ref` 也不得匹配计划中的既有 Worker。
它还会根据实际 `agent_type` 从当前 Agent TOML 读取并记录：

- `configured_model`；
- `configured_model_reasoning_effort`；
- `configured_sandbox_mode`；
- `profile_source: agent_toml` 和对应配置文件名。

这些字段是角色配置证据，不是运行时遥测。只有运行时接口直接回报并提供可验证来源时，才能另行声明为
运行时确认；不得从 `runtime_ref`、任务名、nickname、Worker 自述或历史记录推断模型和推理档位。
文件变化本身也不证明当前 Codex 会话已经重新加载角色配置。

完成当前用户任务前，按实际执行时间顺序把本任务产生的全部 WorkPlan / execution 对聚合为一个确定性的
`SUBAGENT_EXECUTION_DIGEST`：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  digest \
  --entry /tmp/plan-a.json /tmp/plan-a.execution.json \
  --entry /tmp/plan-b.json /tmp/plan-b.execution.json \
  --json \
  --output /tmp/subagent-execution-digest.json
```

Digest 至少聚合：

- 各 `agent_type` 对应的配置模型、推理档位、sandbox、执行尝试数、验收通过数和独立复核数；
- 计划校验数、运行时终态分布、任务验收结果分布、未执行 ready task、残留活跃 Worker；
- 写入证据的 true / false / unknown 计数；
- superseded Worker 去重结果，以及未验收通过、尚未验收、未执行、残留活跃 Worker 和未知写入证据等异常列表。

用户可见回复的语言、结构和篇幅遵循当前会话的 `developer_instructions`。正常情况下只展示紧凑的
会话级子任务执行概览，不逐字粘贴完整 `WORKPLAN_EXECUTION_SUMMARY`，也不使用 `<details>` 隐藏原始摘要。
用户明确要求原始审计，或存在失败、未执行 ready task、残留活跃 Worker、未知写入证据、配置冲突等异常时，
再提供相应的详细记录。

## 派发合同

派发内容按任务需要包含以下信息，不要求机械套用固定模板：

- 每次 Worker 执行唯一的 `task_id` 和当前 `attempt`；
- 明确目标和可独立验收的交付物；
- 完成任务所需的最小背景；
- 允许执行的操作；
- 文件、目录、模块或职责所有权；
- 必须保持的合同、兼容性和项目约束；
- 需要执行或提供的验证证据；
- 明确排除的范围。

写代理必须知道：

- 它并非独自在代码库中工作；
- 只能修改明确分配的所有权范围；
- 必须保留用户和其他代理的改动；
- 不得还原、覆盖或整理无关内容；
- 多个写代理只有在所有权不重叠时才能并行；
- 发现必要修改超出所有权时，应报告范围冲突，不自行扩张。

只读代理不得创建、修改或删除文件，也不得改变系统或外部状态。

复核代理只报告发现，不直接修复；由拥有写入所有权的代理或主代理处理修复。

要求代理返回：

- 与派发一致的 `task_id` 和 `attempt`；
- 实际结果；
- 检查或修改范围；
- 关键证据；
- 实际执行的验证；
- 尚未确认的缺口；
- 影响下一步决策的阻塞。

代码结论提供准确的 `file:line`。否定结论必须说明实际搜索范围、入口或关键词。
不得把计划中的检查、未运行的测试或推测写成已经完成。

## 角色不匹配

所有自定义角色都使用 `ROLE_MISMATCH` 协议。

子代理发现核心交付不符合其角色前置条件时：

1. 不自行改做相邻角色；
2. 先完成仍在本角色范围内、且不依赖错误部分的工作；
3. 返回 `ROLE_MISMATCH`；
4. 说明不匹配的具体原因和证据；
5. 说明已经完成的范围内工作；
6. 建议更合适的角色；
7. 说明继续工作所需的最小补充信息。

主代理收到 `ROLE_MISMATCH` 后，根据证据最多重新路由一次。第二次仍不匹配时，
停止角色间转派，由主代理重新检查任务拆分、角色前置条件和交付合同。

## Worker 生命周期

- 每个逻辑子任务最多执行 2 次 attempt。每次实际创建或复用 Worker 都计为一次；
  `ROLE_MISMATCH` 后重新路由也计入第二次 attempt。
- 首次执行使用 `attempt: 1`。retry 必须使用全新 `task_id`，声明 `replaces_task_id`，
  且 `attempt` 等于旧 attempt + 1。
- replacement 前旧 attempt Worker 必须已经非活跃。若旧 Worker 仍为 `pending` / `running`，必须先真实 interrupt/stop 或等待其进入非活跃状态；不得让两个 attempt 同时运行。
- 旧 Worker 已为 `completed` / `accepted` / 其他非活跃状态时，不要求当前 Surface 不存在的 stop/reclaim acknowledgement。Planner 通过新计划建立编排边界：将旧 Worker 的 `superseded_by_task_id` 设置为新 `task_id`，并要求 replacement 使用 fresh Worker、全新内部 `worker_id`、不复用任何计划已知的非空 `runtime_ref`，且 `reuse_worker_id: null`。
- `superseded_by_task_id` 不改变运行时事实。旧 Worker 可以仍显示 `completed`、仍具有技术上的 follow-up 能力，但主代理不得再向其发送消息，也不得在后续计划中复用。`retired_from_followup` 继续只记录直接运行时 retirement evidence，两者不得混写。
- 第一次失败本身不触发更昂贵角色。只有新证据满足另一角色的前置条件时才重新路由；
  否则在相同职责内修正任务包、证据、权限或环境后进行第二次 attempt。
- Worker 运行中，如果预期新增信息价值已低于继续运行和整合成本，且不承担必要的安全判断、
  独立复核或不可替代验收职责，允许 early stop。保留已取得证据并将结果标记为不完整。
- Worker 结果已被采纳且不再需要 steering 时，停止继续交互并回收 Worker；不要为了保持线程活跃
  发送无意义消息。
- 当前并发只统计 `pending` / `running` Worker。历史 `completed`、`accepted`、`failed`、
  `stopped`、`interrupted` 或 `reclaimed` Worker 不作为累计数量扣除槽位。
- 复用只用于同一工作流、相同角色、上下文仍有净价值、身份和所有权清楚，且
  `retired_from_followup: false`、`superseded_by_task_id: null` 的 Completed Worker。已被 replacement supersede 或已有直接 retirement evidence 的 Worker 不得复用。
  复用任务仍使用新 `task_id` 和新的验收条件。
- 若运行时提供可验证的 token 或用量数据，复用 Worker 只统计本轮新增区间；无法验证时标记
  不可用，不把历史生命周期累计当成本轮用量。
- 独立复核必须由 fresh 复核 Worker 执行，不得复用原实现 Worker、原调试 Worker 或其上下文线程；
  新内部 `worker_id` 不能掩盖已知 `runtime_ref` 的复用。
- 生命周期状态、retry、复用和独立复核隔离由 WorkPlan Validator 做可机械验证的部分；
  early stop、结果采纳和回收由主代理根据任务价值与验收责任决定。

## 推进与整合

- 不重复子代理已经完成的调查；只核验会影响修改目标、权限边界或存在明显疑点的证据。
- 子代理摘要不能替代主代理对实际待改权威单元、相关合同和最终 diff 的理解。
- 优先利用完成通知；只在下一步确实依赖结果时等待。
- 超时、空输出或未变化状态不代表完成或失败。
- 子代理报告阻塞、所有权冲突或合同不确定性时，先解决边界问题，再继续修改。
- 整合多个结果时检查结论是否互相冲突；冲突未解决前不要把任一结论当作事实。
- 写入工作整合后，检查实际 diff、文件所有权、重复逻辑、隐藏回退、数据合同、
  用户改动和相关验证。

## 独立复核

按实际风险选择复核角色。

涉及公开合同、持久化数据、权限、并发、迁移、兼容性或发布门禁的候选变更，
使用 `critical_reviewer`。

其他需要独立检查的普通候选变更，使用 `reviewer`。

复核者应收到：

- 原始任务和验收要求；
- 候选 diff；
- 相关合同和权威实现；
- 原始验证证据；
- 已知限制和剩余风险。

缺少适用的独立复核角色，或复核无法完成时，应报告门禁缺口，不把主代理自查称为
独立复核。


