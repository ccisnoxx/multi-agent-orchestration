---
name: multi-agent-orchestration
description: 为适合独立并行、上下文隔离或独立复核的 Codex 任务选择固定 Profile 子代理，建立完整任务合同、文件所有权、依赖、生命周期、低通信等待和持久化可管理的审计 Bundle。用于用户明确要求委派，或项目规则已授权且委派具有实际收益时；小型、紧耦合、立即阻塞或本地完成更便宜的工作由主代理直接完成。
---

# 子代理编排

本 Skill 使用**固定角色 Profile**：模型、推理档位、sandbox 和角色行为由当前
`~/.codex/agents/*.toml` 决定。主代理只选择 `agent_type`，不得在派发时覆盖 `model` 或
`reasoning_effort`。

执行链固定为：

```text
用户请求
  → 本 Skill：是否委派、角色、完整任务合同、所有权和通信策略
  → WorkPlan：依赖、容量、冲突、波次、身份和派发合同
  → Agent TOML：固定 model、reasoning effort、sandbox 和角色行为
  → 主代理：实际派发、低通信等待、验收、整合与独立复核
  → Audit Bundle：持久化 plan、dispatch、execution、summary、digest 和版本清单
  → Summary / Digest：机器审计与用户可见概览
```

Planner 不创建 Worker、不调用模型、不联网、不修改目标仓库，也不选择或覆盖模型。

## 是否委派

至少满足一个条件时才委派：

- 存在可独立推进且不会立即阻塞主线程的工作支线；
- 存在所有权不重叠、可安全并行的实现工作；
- 大量外围调查会显著污染主线程上下文；
- 风险边界要求 fresh、只读的独立复核；
- 用户明确要求使用子代理。

以下工作由主代理直接完成：

- 小型、紧耦合、状态敏感或立即阻塞的工作；
- 无法定义独立交付物或可靠所有权的工作；
- 调查结果会立即决定下一行修改；
- 委派、等待和整合成本接近或高于本地完成成本。

委派后，主代理继续推进无依赖且不触碰 Worker 所有权的工作，不因等待重复执行同一任务。

## 路由顺序

命中明确条件后停止；文件多、仓库大、任务描述长或第一次失败都不能单独触发深度角色。

### 已有候选实现或实际 diff，需要复核

- 触及公开合同、持久化数据、权限、并发、迁移、兼容性或发布门禁：
  `critical_reviewer`。
- 其他普通正确性、回归和验证缺口：`reviewer`。

### 没有候选实现，任务只读

- 未来系统边界、状态归属、依赖方向或难以逆转的技术决策：`architect`。
- 现有系统中的并发交错、数据一致性、复杂生命周期、部分失败或故障传播：
  `deep_auditor`。
- 当前系统的跨模块根因、性能瓶颈、非平凡合同或影响分析：`analyst`。
- 文件、符号、入口、配置、调用关系、测试入口或日志归纳：`default`。

### 任务需要写入

已有报错、失败测试、日志、复现步骤或明确错误行为时：

- 同时维持多个相互制约的不变量：`deep_engineer`；
- 其他常规根因定位与修复：`debugger`。

没有既有故障证据时：

- 规则和结果形式完整给定、无需产品或合同判断：`mechanical`；
- 单一清晰输入输出合同的小型脚本、数据处理或独立功能：`utility`；
- 同时维持多个相互制约的不变量：`deep_engineer`；
- 其他验收行为和文件所有权明确的应用功能：`implementer`。

深度角色必须有具体机制支持，例如事务、并发、持久化、迁移、权限、部分失败、生命周期状态机、跨版本兼容或多个公共合同。

## 派发粒度：完整任务闭环

默认派发**可独立验收的完整责任闭环**，不得把同一角色、同一所有权内的普通阶段拆成多次派发。

一个写入 Worker 通常应自行完成：

```text
必要调查 → 实现或修复 → 目标验证 → 修正本次修改导致的问题 → 最终交付
```

只有以下情况才拆分阶段：

- 前置调查可以独立并行；
- 后续需要不同角色、权限或文件所有权；
- 调查结果本身是独立交付物；
- 后续存在真实依赖，当前无法可靠定义任务合同；
- 风险边界要求 fresh 独立复核；
- 上下文规模需要有意隔离。

## 固定派发合同

所有 fresh Worker 必须显式使用：

```json
{
  "task_name": "唯一且表达交付物的名称",
  "agent_type": "明确的固定 Profile 角色",
  "fork_turns": "none",
  "message": "完整、自洽、可独立验收的任务合同"
}
```

固定要求：

- 始终显式指定 `agent_type`；不依赖默认角色；
- fresh Worker 始终显式使用 `fork_turns: "none"`；
- 不传 `model`、`reasoning_effort` 或 `model_reasoning_effort` 覆盖；
- 只派发生成计划中的 `ready_task_ids`；
- task message 必须在没有父线程历史时仍然完整可执行；
- 普通进度不汇报，只在实际阻塞、需要共同决定、所有权冲突、影响其他任务或最终交付时发消息；
- 写代理只修改明确分配的文件、目录、模块或职责范围；
- 复核代理只报告发现，不直接修复。

派发内容按任务需要包含：目标、输入、工作目录、所有权、并行边界、必须保持的合同、验收条件、验证要求、排除范围和通信规则。

## 持久化 Audit Bundle

凡实际创建或复用一个或多个 Worker，必须在首次派发前创建持久化 Audit Bundle。不得把正式审计产物只留在 `/tmp`、会话临时目录或难以发现的任意路径。

默认根目录：

```text
${MULTI_AGENT_AUDIT_ROOT:-${CODEX_HOME:-$HOME/.codex}/audits/multi-agent}
```

创建 Bundle：

```bash
AUDIT_DIR=$(./bin/work-plan audit-init \
  --repo-root "$PWD" \
  --task-name "当前用户任务的简短名称")
```

高风险任务使用 `--risk high`；需要永久保留时使用 `--keep`；普通任务默认保留 14 天，高风险任务默认 90 天。

每个 WorkPlan 阶段先分配稳定文件路径：

```bash
./bin/work-plan audit-stage "$AUDIT_DIR" \
  --name implementation \
  --output /tmp/audit-stage.json
```

只能把该阶段的 draft、generated plan、每个实际 dispatch、execution、summary 写入 `audit-stage` 返回的路径。dispatch 一项一个 JSON，写入返回的 `dispatch_dir`。会话级 Digest 固定写入：

```text
$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json
$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.md
```

全部阶段完成后执行：

```bash
./bin/work-plan audit-finalize "$AUDIT_DIR"
./bin/work-plan audit-verify "$AUDIT_DIR"
```

`audit-finalize` 自动生成或规范化 Markdown Digest 和文本 Summary，记录 Bundle version、Skill release、Planner/schema 版本、artifact 路径、SHA-256、大小、retention、异常状态并关闭 Bundle。关闭后默认视为不可变；修改任何 artifact 都会使 `audit-verify` 失败。

已有临时审计目录可迁移：

```bash
./bin/work-plan audit-import /tmp/existing-audit \
  --repo-root "$PWD" \
  --task-name "原任务名称"
```

查找、审计和删除：

```bash
./bin/work-plan audit-list
./bin/work-plan audit-show AUDIT_ID
./bin/work-plan audit-verify AUDIT_ID
./bin/work-plan audit-delete AUDIT_ID --yes
./bin/work-plan audit-prune
./bin/work-plan audit-prune --apply
```

`audit-prune` 默认 dry-run，只删除 retention 已到期、已关闭、校验通过且无异常的 Bundle。有异常、open 或未验证 Bundle 默认受保护；显式删除它们必须使用 `audit-delete AUDIT_ID --yes --force`。

最终回复在子任务执行概览后提供简短 `audit_id`，不默认暴露本机绝对路径。需要定位时通过 `audit-list` 或 `audit-show` 查找。

## WorkPlan

凡实际创建或复用 Worker，都必须生成并校验 WorkPlan，包括单一、无依赖的 Worker。单任务计划的本地成本很低，但能确保派发合同、配置快照、所有权、执行证据和持久化 Bundle 完整闭合。

以下情况尤其必须在派发前重新规划：

- 同一阶段准备创建两个或更多 Worker；
- 存在一个或多个写入任务；
- 任务之间存在依赖；
- 需要根据当前活跃 Worker 计算容量；
- 需要 retry、复用 Completed Worker 或执行独立复核；
- 新任务可能与活跃 Worker 发生读写冲突；
- 状态、配置、依赖、所有权或验收结果已经变化。

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  --codex-config "${CODEX_CONFIG:-$HOME/.codex/config.toml}" \
  plan "$STAGE_DRAFT" \
  --output "$STAGE_PLAN"

./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  --codex-config "${CODEX_CONFIG:-$HOME/.codex/config.toml}" \
  validate "$STAGE_PLAN"

./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  --codex-config "${CODEX_CONFIG:-$HOME/.codex/config.toml}" \
  guard-dispatch "$STAGE_PLAN" TASK_ID "$STAGE_DISPATCH" \
  --audit "$AUDIT_ID"
```

`guard-dispatch` 是与 Hook 传输格式解耦的派发前门禁。主代理必须先把实际工具参数写入当前 Bundle 对应 stage 的 dispatch 目录，再传入 `--audit`。门禁除了拒绝非 ready task、错误角色、`fork_turns: "all"` 和模型/effort 覆盖，还会拒绝不在持久化 Bundle 中、或 plan 与 dispatch 不属于同一 stage 的调用。不得仅在 `/tmp` 中准备派发证据后绕过此门禁。

WorkPlan schema、字段、执行记录和示例见 `references/work-plan.md`。

## 用户级 PreToolUse Hook

`bin/subagent-spawn-policy-hook` 是无状态的通用 spawn 策略门禁，读取 stdin 中的 Codex
PreToolUse JSON，检查 `spawn_agent`、`Agent` 和以 `spawn_agent` 结尾的 namespaced 工具。
要求 `task_name`、`agent_type` 为非空字符串，`fork_turns` 显式为 `"none"`，并禁止
`tool_input` 内非空的 `model`、`reasoning_effort`、`model_reasoning_effort`；顶层 `model`
是事件元数据，不算派发覆盖。合法调用静默退出 `0`，策略拒绝、非法 JSON 或 spawn
的 `tool_input` 不是 object 时向 stderr 输出原因并退出 `2`；其他工具退出 `0`。

从 `examples/hooks.user.json` 安装到 `~/.codex/hooks.json`，先备份已有文件，command
使用入口的绝对路径。入口权限设为 `755`，配置权限设为 `600`。新会话中必须通过
`/hooks` 审查和信任；不得自动绕过 Hook trust。详细步骤见
[`docs/USAGE_AND_AUDIT_GUIDE.md`](docs/USAGE_AND_AUDIT_GUIDE.md#51-用户级-pretooluse-hook)。

Hook 不读取 WorkPlan、stage 或 audit 状态。WorkPlan `guard-dispatch` 仍负责
ready_task（`ready_task_ids`）、stage、audit 和 fresh/reuse 合同，执行后仍保存
dispatch、execution、Summary 和 Digest。specialized tool path 可能绕过 Hook，
不能因安装 Hook 删除派发前门禁或执行后审计。

## 容量与所有权

- `max_concurrent_workers` 是当前工作流的请求上限；
- Planner 读取 `config.toml` 中的 `agents.max_concurrent_threads_per_session`；
- `effective_capacity` 取两者较小值；
- 只有 `pending` / `running` Worker 占用当前槽位；
- 同波次禁止写写和写读冲突，读读不冲突；
- 与活跃 Worker 所有权冲突的任务保持 blocked；
- 状态、依赖、配置或所有权变化后重新规划，不沿用失效波次。

## Worker 身份和生命周期

- `task_id`：一次逻辑执行的唯一任务 ID；
- `worker_id`：Planner 内部稳定机器标识；
- `runtime_ref`：运行时直接暴露的 opaque Worker 引用；
- `runtime_ref_source`：只允许 `unknown`、`spawn_metadata` 或 `thread_status_metadata`。

不得从 task name、nickname、Worker 自述、TOML 或配置推断运行时身份。

运行时 `status`、任务验收 `task_outcome`、运行时 retirement 和编排 supersession 是不同事实：

- `completed` 不等于任务 `accepted`；
- `retired_from_followup` 只记录 stop/reclaim/明确终态的直接证据；
- fresh replacement 通过 `superseded_by_task_id` 禁止旧 Worker 后续 follow-up 和复用，不伪造运行时状态。

## 复用、retry 与独立复核

相关延续优先复用原 Worker，但必须同时满足：

- 同一工作流和相同角色；
- 原上下文仍有净价值；
- Worker 当前非活跃；
- 所有权不冲突；
- `retired_from_followup: false`；
- `superseded_by_task_id: null`；
- 不属于独立复核。

复用使用 `followup_task`、新的 `task_id` 和新的验收条件；`fork_turns` 不适用，记录为 `null`。

每个逻辑子任务最多两次 attempt。retry 必须：

- 使用新 `task_id`；
- 声明 `replaces_task_id`；
- 使用 fresh Worker；
- 旧 Worker 已非活跃；
- 不复用旧内部 `worker_id` 或已知非空 `runtime_ref`；
- 不因第一次失败自动提升模型成本。先判断是输入、环境、权限、所有权、角色还是实际复杂度问题。

独立复核必须使用 fresh `reviewer` 或 `critical_reviewer`，不得复用实现 Worker 或其上下文线程。

## 低通信等待

子代理普通进度禁止主动汇报。只有实际阻塞、需要主代理决定、所有权冲突、影响其他任务或最终交付时才发消息。不要发送“收到”“正在处理”“没有新进展”等确认或状态消息。

主代理不得连续轮询、催促或因 timeout 重复执行同一任务：

- 有其他独立工作时继续推进，不调用等待；
- 完全依赖异步结果时使用运行时允许的较长有界 `wait_agent`；
- completion notification 到达时立即处理；
- timeout 不表示成功或失败；必要时最多做一次状态对账，再继续长等待；
- 不用 `send_message` 查询普通进度。

## 验收与证据复用

主代理信任可验证证据，但仍承担最终验收责任：

- 不重复 Worker 已执行且相关输入未变化的同一测试、构建、静态检查或调查；
- 失败后先诊断，只有相关代码、配置、环境、依赖或证据变化后才重跑；
- 检查实际 diff、工作树、所有权越界、结果冲突和集成后新增风险；
- 只运行合并后才有意义、Worker 未覆盖或项目明确要求的最终门禁；
- 不用逐文件哈希替代行为测试和 diff 审查。

## 执行审计与最终回复

每个实际派发阶段必须把 dispatch、execution record 和完整 Summary 写入其 Audit Stage 路径。完成当前用户任务前，将 Bundle 内全部计划按执行顺序聚合为 Digest，并关闭、验证 Bundle：

```bash
./bin/work-plan summary \
  "$STAGE_PLAN" "$STAGE_EXECUTION" \
  --json --output "$STAGE_SUMMARY_JSON"

./bin/work-plan digest \
  --entry "$STAGE_A_PLAN" "$STAGE_A_EXECUTION" \
  --entry "$STAGE_B_PLAN" "$STAGE_B_EXECUTION" \
  --json \
  --output "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json"

./bin/work-plan render-digest \
  "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json" \
  --output "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.md"

./bin/work-plan audit-finalize "$AUDIT_DIR"
./bin/work-plan audit-verify "$AUDIT_DIR"
```

`digest` 负责重新验证 plan、execution、Agent TOML 和当前 `config.toml`。JSON Digest 一旦成功生成，即为该次执行的历史审计快照。后续仅为补生成、恢复或重新展示 Markdown 时，必须使用 `render-digest`，不得重新运行 `digest` 使当前配置漂移阻断纯展示，也不得修改旧计划中的配置证据来迎合当前配置。

Execution record 必须记录实际派发方式、`fork_turns`、模型覆盖为空、实际写入路径证据、follow-up、中途消息、等待和状态轮询次数。

最终用户回复正常只展示紧凑的 `SUBAGENT_EXECUTION_DIGEST` 聚合结果。用户可见的“子任务执行概览”必须直接复用本地 renderer 生成的 `SUBAGENT_EXECUTION_DIGEST.md`，不得根据 JSON 手工重建表格、合并表头、改变列顺序、改写统计值或在管道符前添加转义字符。可以在该区块前后补充任务结果和限制，但下列表头与分隔行必须原样保留：

```markdown
| `agent_type` | 模型（Agent TOML） | 推理档位 | 执行尝试 | 验收通过 | 独立复核 |
|---|---|---|---:|---:|---:|
```

提交最终回复前，读取实际生成的 Markdown 文件并确认表头、分隔行和数据行均为六列；若 Markdown Digest 尚未生成，先对已保存的 JSON Digest 运行 `render-digest`，不得重新聚合历史 plan/execution，也不得临时手工拼表。

只有用户明确要求原始审计，或存在未验收通过、尚未验收、未执行 ready task、残留活跃 Worker、未知写入证据或配置冲突时，才展开对应细节。最终回复同时提供 Bundle 的 `audit_id`，使用户能够用 `audit-show`、`audit-verify` 或 `audit-delete` 定位和管理产物。

## 配置检查

修改 Skill、Agent TOML 或 Codex 配置后，使用新会话验证实际加载行为，并运行：

```bash
./bin/work-plan \
  --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
  --codex-config "${CODEX_CONFIG:-$HOME/.codex/config.toml}" \
  doctor
```

文件变化本身不证明当前会话已重新加载配置、角色、Skill 或工具 schema。
