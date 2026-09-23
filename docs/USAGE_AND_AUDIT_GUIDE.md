# multi-agent-orchestration 1.6.0 使用与持久化审计指南

> 适用版本：Skill release `1.6.0`、Planner `1.5.1`、WorkPlan schema `5`、Execution record `6`、Summary schema `3`、Digest schema `3`、Audit Bundle `1`。
>
> 推荐归档位置：仓库中的 `docs/USAGE_AND_AUDIT_GUIDE.md`。

## 1. 项目定位

`multi-agent-orchestration` 是面向 Codex 固定角色 Profile 的多子代理编排 Skill。它把多代理工作流中可机械验证的部分交给本地确定性工具处理，包括：

- 是否适合委派；
- 子代理角色路由；
- 完整任务合同；
- 依赖与波次；
- 并发容量；
- 文件读写所有权；
- fresh spawn、Worker 复用、retry 和独立复核；
- `fork_turns: "none"` 隔离派发；
- 实际派发、写入路径和通信统计；
- 单阶段 Summary 与会话级 Digest；
- 持久化 Audit Bundle 的创建、查找、校验、保留和删除。

该工具自身**不会**：

- 创建或控制 Codex Worker；
- 调用模型；
- 联网；
- 修改目标仓库；
- 替主代理作出任务验收判断；
- 在派发时覆盖角色 TOML 中的模型、推理档位或 sandbox。

实际 Worker 仍由 Codex 主代理通过运行时多代理工具创建、等待、复用和回收。本项目负责为这些操作建立确定性的计划、门禁和审计证据。

---

## 2. 当前本机布局

推荐保持以下目录职责：

```text
/Users/sc/.codex/
├── config.toml
├── agents/
│   ├── default.toml
│   ├── analyst.toml
│   ├── architect.toml
│   ├── deep_auditor.toml
│   ├── reviewer.toml
│   ├── critical_reviewer.toml
│   ├── mechanical.toml
│   ├── utility.toml
│   ├── implementer.toml
│   ├── debugger.toml
│   └── deep_engineer.toml
├── skills/
│   └── multi-agent-orchestration/
└── audits/
    └── multi-agent/
```

核心路径变量：

```bash
export CODEX_HOME=/Users/sc/.codex
export MAO_HOME="$CODEX_HOME/skills/multi-agent-orchestration"
export WP="$MAO_HOME/bin/work-plan"
export AGENTS_DIR="$CODEX_HOME/agents"
export CODEX_CONFIG="$CODEX_HOME/config.toml"
export AUDIT_ROOT="$CODEX_HOME/audits/multi-agent"
```

默认 Audit Root 的解析顺序为：

```text
--audit-root PATH
→ MULTI_AGENT_AUDIT_ROOT
→ CODEX_HOME/audits/multi-agent
→ ~/.codex/audits/multi-agent
```

当前 `doctor` 已确认：

- 固定角色 Profile 数量为 11；
- `agents.max_concurrent_threads_per_session = 4`；
- Audit Root 存在且可写；
- 角色模型、推理档位和 sandbox 均可解析；
- Planner 版本为 `1.5.1`。

---

## 3. 固定角色 Profile

主代理只选择 `agent_type`，不在 `spawn_agent` 时传入模型或 reasoning effort。

| 角色 | 主要用途 | 模型 | 推理档位 | Sandbox |
|---|---|---|---|---|
| `default` | 文件、符号、入口、配置、日志和测试入口定位 | `gpt-5.6-luna` | `high` | `read-only` |
| `analyst` | 解释当前系统的跨模块根因、合同、性能和影响 | `gpt-5.6-sol` | `high` | `read-only` |
| `architect` | 未来边界、状态归属、依赖方向和迁移结构 | `gpt-6-astra` | `high` | `read-only` |
| `deep_auditor` | 并发交错、数据一致性、复杂生命周期和部分失败调查 | `gpt-6-astra` | `medium` | `read-only` |
| `reviewer` | 普通候选变更的正确性、回归和验证缺口复核 | `gpt-5.6-sol` | `high` | `read-only` |
| `critical_reviewer` | 高风险候选变更的独立复核 | `gpt-6-astra` | `medium` | `read-only` |
| `mechanical` | 规则完整给定的机械修改 | `gpt-5.6-luna` | `high` | `workspace-write` |
| `utility` | 单一输入输出合同的小型脚本和数据处理 | `gpt-5.6-luna` | `max` | `workspace-write` |
| `implementer` | 无既有故障前提的常规功能实现 | `gpt-5.6-sol` | `high` | `workspace-write` |
| `debugger` | 已有失败证据的常规故障修复 | `gpt-5.6-sol` | `high` | `workspace-write` |
| `deep_engineer` | 同时维持多个相互制约不变量的复杂实现 | `gpt-6-astra` | `medium` | `workspace-write` |

### 3.1 路由原则

- 有候选 diff 且触及公开合同、数据、权限、并发、迁移、兼容性或发布门禁：`critical_reviewer`。
- 有候选 diff 的普通复核：`reviewer`。
- 解释当前系统：`analyst`。
- 调查复杂状态反例：`deep_auditor`。
- 决定未来架构：`architect`。
- 已有失败证据：`debugger`；复杂多不变量故障：`deep_engineer`。
- 没有故障证据的常规功能：`implementer`。
- 完整规则驱动的机械修改：`mechanical`。
- 单一清晰输入输出的小工具：`utility`。
- 只做事实定位：`default`。

文件多、仓库大、第一次失败或预计耗时长，本身不构成选择深度角色的理由。

---

## 4. 核心能力总览

| 命令 | 能力 | 典型使用时机 |
|---|---|---|
| `doctor` | 检查 Agent TOML、Codex 配置和 Audit Root | 安装、升级或配置变更后 |
| `plan` | 校验 Draft 并生成确定性波次、容量和 ready task | 每次派发前 |
| `validate` | 重新计算并校验 generated plan 未被篡改 | 实际派发前 |
| `guard-dispatch` | 校验实际派发参数并绑定持久化 Audit Stage | Worker 创建或复用前 |
| `summary` | 校验单阶段 execution 并生成完整机器审计 | 每个阶段结束后 |
| `digest` | 聚合多个阶段并重新校验当前配置 | 当前用户任务全部阶段完成后 |
| `render-digest` | 从历史 Digest JSON 纯渲染 Markdown | 补生成或恢复用户可见概览 |
| `audit-init` | 创建持久化 Audit Bundle | 首次派发前 |
| `audit-stage` | 为一个 WorkPlan 阶段分配确定路径 | 每个实现、调查、复核阶段开始前 |
| `audit-finalize` | 补齐可读产物、生成 manifest 和 SHA-256、关闭 Bundle | Digest JSON 已生成后 |
| `audit-verify` | 离线验证已关闭 Bundle 的完整性 | 归档后、删除前或故障排查时 |
| `audit-list` | 统一查找所有 Audit Bundle | 日常发现和清理 |
| `audit-show` | 查看单个 Bundle 的元数据和状态 | 根据 `audit_id` 定位产物 |
| `audit-import` | 将旧 `/tmp` 审计目录迁移到持久化根目录 | 升级旧工作流时 |
| `audit-delete` | 安全删除单个 Bundle | 明确不再需要时 |
| `audit-prune` | 按 retention 预览或批量删除 | 定期维护 |

所有命令成功返回码为 `0`，协议、输入、配置或证据不满足要求时返回 `2`。

---

## 5. 安装后验证

进入 Skill 目录：

```bash
cd /Users/sc/.codex/skills/multi-agent-orchestration
```

检查 Skill 专属 Python：

```bash
./bin/skill-python --print-path
```

运行完整验证：

```bash
./bin/verify-skill
```

运行配置检查：

```bash
./bin/work-plan \
  --agents-dir /Users/sc/.codex/agents \
  --codex-config /Users/sc/.codex/config.toml \
  doctor
```

修改 Skill、Agent TOML 或 `config.toml` 后，应启动新的 Codex 会话再验证实际加载行为。文件变化本身不能证明现有会话已经重新加载。

---

## 6. Audit Bundle 的目录与生命周期

### 6.1 默认目录结构

```text
$CODEX_HOME/audits/multi-agent/
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
        ├── 02-review.draft.json
        ├── 02-review.plan.json
        ├── 02-review.dispatches/
        ├── 02-review.execution.json
        ├── 02-review.summary.json
        ├── 02-review.summary.txt
        ├── SUBAGENT_EXECUTION_DIGEST.json
        └── SUBAGENT_EXECUTION_DIGEST.md
```

目录权限为 `0700`，文件权限为 `0600`。仓库目录名与绝对路径摘要共同形成 repo key，避免同名仓库冲突。

### 6.2 生命周期

```text
audit-init
  → open Bundle
  → audit-stage
  → 写入 draft / plan / dispatch / execution / summary
  → 生成 Digest JSON
  → render-digest
  → audit-finalize
  → closed + verified Bundle
  → audit-list / audit-show / audit-verify
  → audit-delete 或 audit-prune
```

关闭后的 Bundle 默认不可变。增加、删除或修改任何 artifact，都会导致 `audit-verify` 失败。

### 6.3 保留策略

| 类型 | 默认保留 |
|---|---:|
| 普通任务 | 14 天 |
| 高风险任务 | 90 天 |
| 含异常的 Bundle | 至少 90 天 |
| `--keep` | 永久保留 |
| `--retention-days N` | 显式覆盖 |

`audit-prune` 默认仅预览，不会自动删除。

---

## 7. 标准完整工作流

下面是一套可直接改造的两阶段流程：先实现，再做独立复核。

### 7.1 初始化环境变量

```bash
export CODEX_HOME=/Users/sc/.codex
export MAO_HOME="$CODEX_HOME/skills/multi-agent-orchestration"
export WP="$MAO_HOME/bin/work-plan"
export AGENTS_DIR="$CODEX_HOME/agents"
export CODEX_CONFIG="$CODEX_HOME/config.toml"
```

### 7.2 创建持久化 Audit Bundle

建议使用 JSON 输出，便于脚本提取 `audit_id` 和目录：

```bash
INIT_META=$(mktemp)

"$WP" audit-init \
  --repo-root "$PWD" \
  --task-name "修复认证缓存" \
  --json \
  --output "$INIT_META"

AUDIT_ID=$(jq -r '.audit_id' "$INIT_META")
AUDIT_DIR=$(jq -r '.audit_dir' "$INIT_META")

printf 'audit_id=%s\naudit_dir=%s\n' "$AUDIT_ID" "$AUDIT_DIR"
```

高风险任务：

```bash
"$WP" audit-init \
  --repo-root "$PWD" \
  --task-name "执行数据库迁移" \
  --risk high
```

永久保留：

```bash
"$WP" audit-init \
  --repo-root "$PWD" \
  --task-name "发布门禁审计" \
  --keep
```

### 7.3 分配实现阶段的确定路径

```bash
STAGE_A_META=$(mktemp)

"$WP" audit-stage "$AUDIT_ID" \
  --name implementation \
  --output "$STAGE_A_META"

DRAFT_A=$(jq -r '.paths.draft' "$STAGE_A_META")
PLAN_A=$(jq -r '.paths.plan' "$STAGE_A_META")
DISPATCH_DIR_A=$(jq -r '.paths.dispatch_dir' "$STAGE_A_META")
EXECUTION_A=$(jq -r '.paths.execution' "$STAGE_A_META")
SUMMARY_A=$(jq -r '.paths.summary_json' "$STAGE_A_META")
SUMMARY_TEXT_A=$(jq -r '.paths.summary_text' "$STAGE_A_META")
```

`audit-stage` 自动分配单调递增的前缀，例如：

```text
01-implementation
02-review
03-followup
```

### 7.4 写入 Draft WorkPlan

示例：已有失败证据，使用 `debugger` 修复 `src/auth/cache.py`。

```json
{
  "version": 5,
  "plan_id": "auth-cache-fix",
  "max_concurrent_workers": 2,
  "runtime_workers": [],
  "prior_tasks": [],
  "tasks": [
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
      "deliverable": "完成调查、根因修复、目标测试、必要修正和最终交付。",
      "acceptance_criteria": [
        "原失败行为被修复",
        "目标测试通过",
        "不改变无关认证合同"
      ]
    }
  ]
}
```

将其写入 `$DRAFT_A`。

### 7.5 生成并验证计划

```bash
"$WP" \
  --agents-dir "$AGENTS_DIR" \
  --codex-config "$CODEX_CONFIG" \
  plan "$DRAFT_A" \
  --output "$PLAN_A"

"$WP" \
  --agents-dir "$AGENTS_DIR" \
  --codex-config "$CODEX_CONFIG" \
  validate "$PLAN_A" >/dev/null
```

检查当前可立即派发的任务：

```bash
jq '.ready_task_ids, .blocked_task_ids, .blocked_reasons' "$PLAN_A"
```

只允许派发 `ready_task_ids` 中的任务。

### 7.6 写入标准化派发记录

一个任务对应一个 dispatch JSON，保存在该阶段的 `dispatch_dir`：

```bash
TASK_ID=auth-fix-a1
DISPATCH_A="$DISPATCH_DIR_A/$TASK_ID.json"

cat >"$DISPATCH_A" <<'JSON'
{
  "method": "spawn_agent",
  "task_name": "修复认证缓存失效",
  "agent_type": "debugger",
  "fork_turns": "none",
  "model": null,
  "reasoning_effort": null
}
JSON
```

### 7.7 派发前门禁

```bash
"$WP" \
  --agents-dir "$AGENTS_DIR" \
  --codex-config "$CODEX_CONFIG" \
  guard-dispatch \
  "$PLAN_A" \
  "$TASK_ID" \
  "$DISPATCH_A" \
  --audit "$AUDIT_ID"
```

门禁会拒绝：

- Bundle 不存在、已关闭或无效；
- plan 与 dispatch 不在同一 Audit Stage；
- 任务不在 `ready_task_ids`；
- `task_name`、`agent_type` 或 method 与计划不一致；
- fresh spawn 未显式使用 `fork_turns: "none"`；
- 模型或 reasoning effort 被覆盖；
- follow-up 与 fresh spawn 的合同混用。

`guard-dispatch` 只负责验证。真正的 `spawn_agent` 或 `followup_task` 仍由 Codex 运行时执行。

### 7.8 主代理执行 Worker 并保存 Execution record

Worker 完成后，主代理依据实际工具结果和验收条件写入 `$EXECUTION_A`。

```json
{
  "version": 6,
  "plan_id": "auth-cache-fix",
  "plan_command": [
    "./bin/work-plan",
    "plan",
    "01-implementation.draft.json",
    "--output",
    "01-implementation.plan.json"
  ],
  "validate_command": [
    "./bin/work-plan",
    "validate",
    "01-implementation.plan.json"
  ],
  "validate_status": "passed",
  "workers": [
    {
      "task_id": "auth-fix-a1",
      "agent_type": "debugger",
      "worker_id": "worker-auth-fix-a1",
      "runtime_ref": null,
      "runtime_ref_source": "unknown",
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
  ],
  "active_worker_ids_after_execution": [],
  "writes_observed": true,
  "communication": {
    "wait_call_count": 1,
    "wait_timeout_count": 0,
    "status_poll_count": 1
  }
}
```

关键规则：

- `completed` 只是运行时状态，不自动等于 `accepted`；
- `task_outcome=accepted` 只能由主代理实际检查交付物和验收条件后填写；
- `runtime_ref` 未从运行时直接取得时，必须记录 `null / unknown`；
- `observed_write_paths` 必须来自实际证据，且不能越过计划所有权；
- 证据不足时使用 `null`，不能伪造空列表；
- communication 记录实际等待、超时和状态观察次数。

### 7.9 生成阶段 Summary

JSON 机器审计：

```bash
"$WP" \
  --agents-dir "$AGENTS_DIR" \
  --codex-config "$CODEX_CONFIG" \
  summary "$PLAN_A" "$EXECUTION_A" \
  --json \
  --output "$SUMMARY_A"
```

人类可读文本可立即生成，也可由 `audit-finalize` 补生成：

```bash
"$WP" \
  --agents-dir "$AGENTS_DIR" \
  --codex-config "$CODEX_CONFIG" \
  summary "$PLAN_A" "$EXECUTION_A" \
  --output "$SUMMARY_TEXT_A"
```

### 7.10 创建独立复核阶段

分配第二个阶段：

```bash
STAGE_B_META=$(mktemp)

"$WP" audit-stage "$AUDIT_ID" \
  --name review \
  --output "$STAGE_B_META"
```

然后按相同方式写入：

- `02-review.draft.json`；
- `02-review.plan.json`；
- `02-review.dispatches/<task-id>.json`；
- `02-review.execution.json`；
- `02-review.summary.json`。

独立复核任务必须满足：

```json
{
  "agent_type": "reviewer",
  "independent_review": true,
  "review_of_task_ids": ["auth-fix-a1"],
  "reuse_worker_id": null,
  "dispatch_method": "spawn_agent",
  "fork_turns": "none"
}
```

高风险变更使用 `critical_reviewer`。

### 7.11 聚合会话级 Digest

所有阶段按实际执行顺序传入：

```bash
DIGEST_JSON="$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json"
DIGEST_MD="$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.md"

"$WP" \
  --agents-dir "$AGENTS_DIR" \
  --codex-config "$CODEX_CONFIG" \
  digest \
  --entry "$PLAN_A" "$EXECUTION_A" \
  --entry "$PLAN_B" "$EXECUTION_B" \
  --json \
  --output "$DIGEST_JSON"
```

`digest` 会重新读取当前 Agent TOML 和 `config.toml`，发现角色或配置漂移。JSON Digest 是该次执行的机器事实快照。

从 JSON 快照渲染 Markdown：

```bash
"$WP" render-digest \
  "$DIGEST_JSON" \
  --output "$DIGEST_MD"
```

历史 Digest 已生成后，若 Markdown 丢失，只运行 `render-digest`。不要重新运行 `digest` 来迎合当前配置，也不要修改旧 plan 中的配置证据。

### 7.12 关闭并验证 Bundle

```bash
"$WP" audit-finalize "$AUDIT_ID"
"$WP" audit-verify "$AUDIT_ID"
```

`audit-finalize` 会：

- 补生成 Digest Markdown；
- 补生成 Summary text；
- 检查 plan、execution、summary 和 Digest 的 `plan_id` 关联；
- 检查 execution 中每个 Worker 都有匹配的 dispatch；
- 检查执行数、验收数和独立复核数；
- 检查 Markdown 等于 JSON 的确定性 renderer 输出；
- 记录 artifact 路径、类型、大小和 SHA-256；
- 收紧权限；
- 将 Bundle 从 `open` 改为 `closed`。

`audit-verify` 不读取当前 Agent TOML 或 `config.toml`，只根据 manifest 对关闭后的 Bundle 做离线完整性校验。

### 7.13 最终回复

最终回复应：

1. 先说明任务结果和验证结果；
2. 直接复用 `SUBAGENT_EXECUTION_DIGEST.md` 中的“子任务执行概览”；
3. 提供简短的 `audit_id`；
4. 不默认暴露本机绝对路径；
5. 不根据 JSON 手工重建表格。

示例：

```text
审计 ID：20260923T062456Z-auth-cache-fix-a07c8785
```

用户之后可以通过 `audit-show`、`audit-verify` 或 `audit-delete` 管理该任务产物。

---

## 8. 统一查找和查看所有子代理产物

### 8.1 列出全部 Bundle

```bash
"$WP" audit-list
```

JSON 输出：

```bash
"$WP" audit-list --json
```

只看最近 20 个：

```bash
"$WP" audit-list --limit 20
```

按状态过滤：

```bash
"$WP" audit-list --status open
"$WP" audit-list --status closed
"$WP" audit-list --status invalid
```

`audit-list` 会显示：

- `audit_id`；
- Bundle、Skill 和 Planner 版本；
- open、closed 或 invalid 状态；
- 是否需要关注；
- 删除日期；
- 仓库和任务名；
- 计划数和验收数；
- 占用空间；
- 实际目录。

### 8.2 查看单个 Bundle

命令接受 `audit_id` 或绝对路径：

```bash
"$WP" audit-show AUDIT_ID
"$WP" audit-show /Users/sc/.codex/audits/multi-agent/.../AUDIT_ID
```

JSON：

```bash
"$WP" audit-show AUDIT_ID --json
```

### 8.3 在 Finder 中查看

```bash
open "$AUDIT_ROOT"
```

### 8.4 根据 ID 定位真实目录

```bash
AUDIT_PATH=$("$WP" audit-show AUDIT_ID --json | jq -r '.audit_dir')
open "$AUDIT_PATH"
```

---

## 9. 验证和防篡改

关闭 Bundle 后运行：

```bash
"$WP" audit-verify AUDIT_ID
```

验证内容包括：

- Bundle 已关闭；
- manifest verification 状态为 passed；
- 每个 artifact 仍存在；
- artifact 数量与 manifest 一致；
- 文件大小和 SHA-256 未变化；
- plan、execution、summary 和 Digest 的关联一致；
- dispatch 与 execution 的实际派发记录一致；
- Markdown Digest 与 JSON renderer 输出一致；
- 没有 symlink。

若归档后手工编辑任何文件，`audit-verify` 将失败。不要修改已关闭 Bundle；需要修正时应新建任务或保留原 Bundle 并记录异常。

---

## 10. 迁移旧的 `/tmp` 审计目录

例如已有：

```text
/tmp/mao-write-smoke-audit
```

执行：

```bash
IMPORT_META=$(mktemp)

"$WP" audit-import \
  /tmp/mao-write-smoke-audit \
  --repo-root /private/tmp/mao-write-smoke \
  --task-name "calc.add smoke test" \
  --retention-days 7 \
  --json \
  --output "$IMPORT_META"

IMPORTED_AUDIT_ID=$(jq -r '.audit_id' "$IMPORT_META")

"$WP" audit-show "$IMPORTED_AUDIT_ID"
"$WP" audit-verify "$IMPORTED_AUDIT_ID"
```

只有验证通过后才删除旧目录：

```bash
rm -rf /tmp/mao-write-smoke-audit
```

`audit-import`：

- 复制普通文件；
- 拒绝 symlink；
- 忽略源目录中的旧 `manifest.json`；
- 在持久化根目录创建新的 Bundle 1 manifest；
- 尝试直接 finalize 和关闭；
- 旧目录没有 `audit-stage` 元数据时可能产生 warning，但完整的 plan、execution、summary 和 Digest 仍可被发现和管理。

---

## 11. 安全删除和批量清理

### 11.1 删除单个正常 Bundle

先验证：

```bash
"$WP" audit-verify AUDIT_ID
```

再删除：

```bash
"$WP" audit-delete AUDIT_ID --yes
```

默认只允许删除：

- `closed`；
- manifest 已验证；
- 当前再次验证仍通过；
- 没有异常或 attention 标记。

### 11.2 强制删除

只有明确接受丢失审计证据时使用：

```bash
"$WP" audit-delete AUDIT_ID --yes --force
```

适用场景：

- open Bundle 已确认废弃；
- manifest 损坏；
- Bundle 已被篡改且不再需要；
- 含异常 Bundle 已完成外部归档。

### 11.3 批量清理

默认 dry-run：

```bash
"$WP" audit-prune
```

确认选择结果后实际删除：

```bash
"$WP" audit-prune --apply
```

默认跳过：

- open Bundle；
- 未验证或当前校验失败的 Bundle；
- 未到删除日期的 Bundle；
- `--keep` Bundle；
- 含异常的 attention Bundle。

确实需要让已过期 attention Bundle 进入选择范围：

```bash
"$WP" audit-prune --include-attention
"$WP" audit-prune --include-attention --apply
```

建议始终先执行不带 `--apply` 的预览。

---

## 12. 各产物的用途和保留建议

| Artifact | 用途 | 保留建议 |
|---|---|---|
| `*.draft.json` | 主代理提交给 Planner 的原始任务合同 | 调试和回溯时有价值 |
| `*.plan.json` | 确定性生成的容量、依赖、波次和派发计划 | 建议保留 |
| `*.dispatches/*.json` | 实际派发参数标准化记录 | 建议保留 |
| `*.execution.json` | Worker 状态、任务验收、写入路径和通信证据 | 强烈建议保留 |
| `*.summary.json` | 单阶段完整机器审计 | 建议保留 |
| `*.summary.txt` | 单阶段人类可读详情 | 可选，但 finalize 会生成 |
| `SUBAGENT_EXECUTION_DIGEST.json` | 会话级机器事实源 | 强烈建议保留 |
| `SUBAGENT_EXECUTION_DIGEST.md` | 最终用户可见概览 | 建议保留 |
| `manifest.json` | Bundle 版本、retention、清单、SHA-256 和验证状态 | 必须保留 |

工作代码、测试和文档仍留在业务项目仓库中；Audit Bundle 只保存多代理执行证据，不替代 Git diff、测试结果或项目历史。

默认不要把真实 Bundle 提交到公开 GitHub，因为其中可能包含：

- 本机路径；
- 私有仓库结构；
- 任务名称；
- Worker 内部标识；
- 命令参数；
- 配置摘要；
- 未来可能增加的运行时引用。

需要公开示例时，应脱敏并转换成 `examples/` 或测试 fixture。

---

## 13. Worker 复用、Retry 和独立复核

### 13.1 Worker 复用

只有同时满足以下条件才使用 `followup_task`：

- 同一工作流；
- 相同角色；
- 原上下文仍有净价值；
- Worker 当前非活跃；
- 所有权不冲突；
- 未 retirement；
- 未被 supersede；
- 不是独立复核；
- 不是 replacement retry。

复用任务仍使用新的 `task_id` 和验收条件：

```json
{
  "dispatch_method": "followup_task",
  "fork_turns": null,
  "reuse_worker_id": "worker-existing",
  "accounting_scope": "current_attempt_delta"
}
```

### 13.2 Retry

每个逻辑子任务最多两次 attempt。Retry 必须：

- 新 `task_id`；
- `attempt = prior attempt + 1`；
- 声明 `replaces_task_id`；
- 使用 fresh Worker；
- 旧 Worker 已非活跃；
- 不复用旧内部 `worker_id`；
- 不复用任何已知非空 `runtime_ref`；
- 不因第一次失败自动提升模型成本。

### 13.3 独立复核

必须：

- 使用 fresh `reviewer` 或 `critical_reviewer`；
- `independent_review: true`；
- `review_of_task_ids` 非空；
- `reuse_worker_id: null`；
- `fork_turns: "none"`；
- 不继承实现 Worker 的线程。

---

## 14. 低通信和等待策略

子代理只在以下情况下发送中途消息：

- 实际阻塞；
- 需要主代理作出会改变范围、合同或风险的决定；
- 所有权冲突；
- 新证据使计划失效；
- 影响其他并行任务；
- 最终交付。

禁止普通状态消息，例如：

```text
收到
开始处理
还在运行
没有新进展
快完成了
```

主代理：

- 有独立工作时继续推进；
- 完全依赖结果时使用较长有界 `wait_agent`；
- 不连续短轮询；
- timeout 不表示成功或失败；
- 必要时最多做一次状态对账，然后继续等待；
- 不因等待而自己重复执行同一任务。

Execution record 会统计：

```text
parent_followup_count
worker_intermediate_message_count
wait_call_count
wait_timeout_count
status_poll_count
```

Digest 会聚合这些指标，帮助识别高频通讯和重复等待。

---

## 15. 常见故障排查

### 15.1 `generated plan field does not match deterministic output: codex_config_evidence`

原因：历史 plan 保存的 `config.toml` SHA-256 与当前文件不同。

处理：

- 如果只是要恢复 Markdown，使用：

  ```bash
  "$WP" render-digest DIGEST.json --output DIGEST.md
  ```

- 不要修改旧 plan 中的配置证据；
- 不要为了展示 Markdown 重新运行历史 `digest`。

### 15.2 `guard-dispatch` 拒绝 plan 或 dispatch 不在 Bundle 中

原因：文件仍在 `/tmp` 或不属于同一个 `audit-stage`。

处理：

- 先运行 `audit-stage`；
- 使用其返回的 `plan` 和 `dispatch_dir`；
- 再运行 `guard-dispatch --audit AUDIT_ID`。

### 15.3 `audit-finalize` 返回 failed

处理顺序：

```bash
"$WP" audit-show AUDIT_ID --json | jq '.verification'
"$WP" audit-list --status open
```

根据错误补齐：

- draft；
- generated plan；
- dispatch；
- execution；
- summary JSON；
- Digest JSON。

Bundle 校验通过前不会关闭，可修正文件后再次运行 `audit-finalize`。

### 15.4 `audit-verify` 发现 checksum mismatch

说明关闭后 artifact 被修改。不要覆盖 manifest 哈希。

- 若需保留证据：保留原 Bundle 并调查修改来源；
- 若任务需要重新执行：创建新 Bundle；
- 若明确废弃：`audit-delete --yes --force`。

### 15.5 找不到历史子代理产物

```bash
"$WP" audit-list --limit 50
"$WP" audit-list --status open
"$WP" audit-list --status invalid
```

知道 `audit_id` 时：

```bash
"$WP" audit-show AUDIT_ID
```

旧产物若仍在 `/tmp`：

```bash
"$WP" audit-import /tmp/旧目录 --repo-root "$PWD" --task-name "原任务"
```

### 15.6 Markdown 表格损坏

不要根据 JSON 手工拼接。重新渲染：

```bash
"$WP" render-digest \
  "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json" \
  --output "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.md"
```

最终回复直接复用该 Markdown。

---

## 16. 日常运维清单

### 每次使用子代理前

- [ ] 已加载 `multi-agent-orchestration`；
- [ ] 已选择明确 `agent_type`；
- [ ] 已创建 Audit Bundle；
- [ ] 已为当前阶段运行 `audit-stage`；
- [ ] Draft 使用 schema 5；
- [ ] fresh spawn 使用 `fork_turns: "none"`；
- [ ] 未传模型或 reasoning effort 覆盖；
- [ ] 写入所有权精确且不重叠；
- [ ] 只派发 `ready_task_ids`；
- [ ] `guard-dispatch --audit` 通过。

### 每个阶段结束后

- [ ] 保存 execution version 6；
- [ ] 主代理实际检查交付物后填写 `task_outcome`；
- [ ] 记录 `observed_write_paths`；
- [ ] 记录通信统计；
- [ ] 生成 Summary JSON；
- [ ] 如有下一阶段，重新规划。

### 当前用户任务结束前

- [ ] 按执行顺序生成 Digest JSON；
- [ ] 从 JSON 渲染 Digest Markdown；
- [ ] 运行 `audit-finalize`；
- [ ] 运行 `audit-verify`；
- [ ] 最终回复直接复用 Markdown；
- [ ] 最终回复提供 `audit_id`；
- [ ] 不默认暴露绝对路径。

### 定期维护

```bash
"$WP" audit-list --limit 50
"$WP" audit-list --status open
"$WP" audit-list --status invalid
"$WP" audit-prune
```

确认后：

```bash
"$WP" audit-prune --apply
```

---

## 17. 命令速查

```bash
# 配置检查
"$WP" --agents-dir "$AGENTS_DIR" --codex-config "$CODEX_CONFIG" doctor

# 创建 Bundle
"$WP" audit-init --repo-root "$PWD" --task-name "任务"

# 分配阶段
"$WP" audit-stage AUDIT_ID --name implementation

# 计划与校验
"$WP" --agents-dir "$AGENTS_DIR" --codex-config "$CODEX_CONFIG" \
  plan DRAFT.json --output PLAN.json
"$WP" --agents-dir "$AGENTS_DIR" --codex-config "$CODEX_CONFIG" \
  validate PLAN.json

# 派发门禁
"$WP" --agents-dir "$AGENTS_DIR" --codex-config "$CODEX_CONFIG" \
  guard-dispatch PLAN.json TASK_ID DISPATCH.json --audit AUDIT_ID

# 单阶段审计
"$WP" --agents-dir "$AGENTS_DIR" --codex-config "$CODEX_CONFIG" \
  summary PLAN.json EXECUTION.json --json --output SUMMARY.json

# 会话级 Digest
"$WP" --agents-dir "$AGENTS_DIR" --codex-config "$CODEX_CONFIG" \
  digest --entry PLAN.json EXECUTION.json --json --output DIGEST.json

# 纯渲染 Markdown
"$WP" render-digest DIGEST.json --output DIGEST.md

# 关闭与验证
"$WP" audit-finalize AUDIT_ID
"$WP" audit-verify AUDIT_ID

# 发现与查看
"$WP" audit-list
"$WP" audit-show AUDIT_ID

# 迁移旧目录
"$WP" audit-import /tmp/旧审计目录 --repo-root "$PWD" --task-name "原任务"

# 安全删除
"$WP" audit-delete AUDIT_ID --yes

# 批量清理
"$WP" audit-prune
"$WP" audit-prune --apply
```

---

## 18. 推荐落地原则

> **工作代码留在项目仓库；多代理机器审计留在 `$CODEX_HOME/audits/multi-agent`；JSON 是权威机器事实源；用户可见内容使用由 JSON 确定性渲染的 Markdown；普通审计产物不默认提交 GitHub；高风险或异常任务按项目策略延长保留或永久归档。**
