# multi-agent-orchestration

面向 Codex 固定角色 Profile 的多子代理编排 Skill，以及本地确定性的 WorkPlan、执行审计和持久化 Audit Bundle 生命周期工具。

详细使用、完整工作流、Audit Bundle 管理、产物归档和清理方法见 [`docs/USAGE_AND_AUDIT_GUIDE.md`](docs/USAGE_AND_AUDIT_GUIDE.md)。

## 1.6.0 重点

- 所有实际子代理任务先创建持久化 Audit Bundle，默认位于 `$CODEX_HOME/audits/multi-agent`；
- `audit-stage` 为每个 WorkPlan 阶段分配稳定、可发现的 draft、plan、dispatch、execution 和 summary 路径；
- `audit-finalize` 自动生成 Markdown Digest、文本 Summary、文件清单、schema 版本、SHA-256 和关闭状态；
- `audit-list`、`audit-show`、`audit-verify` 提供发现、查看和完整性审计；
- `audit-delete` 使用显式确认安全删除，默认保护 open、未验证或有异常的 Bundle；
- `audit-prune` 按 retention 执行 dry-run 或批量清理；
- `audit-import` 可把既有 `/tmp` 审计目录迁移到持久化存储；
- 普通成功任务默认保留 14 天，高风险任务默认 90 天；异常 Bundle 会自动延长到至少 90 天；
- 固定角色模型、推理档位和 sandbox，不允许 spawn 时临时覆盖；
- fresh Worker 强制 `fork_turns: "none"`，并提供 `guard-dispatch` 门禁；
- `guard-dispatch` 必须绑定 open Audit Bundle，且 plan/dispatch 必须属于同一已分配 stage；
- Digest JSON 是机器事实源，Markdown 只从该快照确定性渲染。

## 目录职责

- `SKILL.md`：委派、路由、完整任务合同、生命周期、通信和验收流程；
- `scripts/work_plan.py`：确定性计划、配置、身份、执行证据和摘要校验；
- `references/work-plan.md`：完整协议；
- `examples/`：可实际运行的 schema 5/6 示例；
- `tests/`：单元与端到端测试；
- `scripts/install_macos.sh`：保留目标 `.git` 的 macOS 原位替换脚本，并初始化私有 Audit Root。

## 当前版本

```text
Skill release           1.6.0
Planner                 1.5.1
WorkPlan schema         5
Execution record        6
Summary schema          3
Digest schema           3
Doctor report           2
Audit Bundle            1
```

## Python

支持 Python 3.10 及以上。Python 3.11+ 使用标准库 `tomllib`；Python 3.10 根据 `requirements.txt` 使用 `tomli`。

推荐专用环境：

```bash
uv python install 3.12
uv venv "$HOME/.local/share/multi-agent-orchestration/.venv" --python 3.12 --managed-python
uv pip install \
  --python "$HOME/.local/share/multi-agent-orchestration/.venv/bin/python" \
  -r requirements.txt
```

## 验证

不要直接使用 macOS 系统自带的 `python3`。统一通过 Skill 的运行时选择器执行：

```bash
./bin/verify-skill
```

该命令会优先使用：

1. `MULTI_AGENT_ORCHESTRATION_PYTHON`；
2. Skill 目录下的 `.venv/bin/python`；
3. `$HOME/.local/share/multi-agent-orchestration/.venv/bin/python`；
4. `uv python find 3.12`；
5. 版本不低于 3.10 的 `python3`。

查看实际选中的解释器：

```bash
./bin/skill-python --print-path
```

只重新生成示例：

```bash
./bin/regenerate-examples
```

## 持久化审计目录

默认根目录：

```text
${MULTI_AGENT_AUDIT_ROOT:-${CODEX_HOME:-$HOME/.codex}/audits/multi-agent}
```

每个实际使用子代理的用户任务先创建一个版本化 Bundle：

```bash
AUDIT_DIR=$(./bin/work-plan audit-init \
  --repo-root "$PWD" \
  --task-name "修复认证缓存")
```

输出目录类似：

```text
~/.codex/audits/multi-agent/<repo-key>/<timestamp>-<task-slug>-<id>/
```

为每个计划阶段分配稳定路径：

```bash
./bin/work-plan audit-stage "$AUDIT_DIR" \
  --name implementation \
  --output /tmp/audit-stage.json
```

`audit-stage` 返回：

```text
<stage>.draft.json
<stage>.plan.json
<stage>.dispatches/<task-id>.json
<stage>.execution.json
<stage>.summary.json
<stage>.summary.txt
```

完成全部阶段并生成 Digest JSON 后关闭 Bundle：

```bash
./bin/work-plan audit-finalize "$AUDIT_DIR"
./bin/work-plan audit-verify "$AUDIT_DIR"
```

`audit-finalize` 会生成或规范化 Markdown Digest、文本 Summary、manifest、artifact SHA-256、schema 版本和 retention 信息，并把目录权限收紧为本机用户可读。

发现和管理：

```bash
./bin/work-plan audit-list
./bin/work-plan audit-show AUDIT_ID
./bin/work-plan audit-verify AUDIT_ID
./bin/work-plan audit-delete AUDIT_ID --yes
./bin/work-plan audit-prune             # dry-run
./bin/work-plan audit-prune --apply     # 删除已到期、已关闭、校验通过且无异常的 Bundle
```

有异常、未关闭或未验证的 Bundle 默认不能删除；确有需要时显式使用：

```bash
./bin/work-plan audit-delete AUDIT_ID --yes --force
```

迁移已有临时目录：

```bash
./bin/work-plan audit-import /tmp/mao-write-smoke-audit \
  --repo-root /private/tmp/mao-write-smoke \
  --task-name "calc.add smoke test"
```

Retention：普通任务默认 14 天，高风险任务默认 90 天，`--keep` 永久保留，`--retention-days N` 可覆盖。`audit-prune` 默认只预览，避免误删。

## 用户可见 Digest

使用 WorkPlan 并实际派发 Worker 时，先生成经过完整重新校验的机器审计 JSON，再从该不可变快照生成用户可见 Markdown：

```bash
./bin/work-plan digest \
  --entry PLAN-A.json EXECUTION-A.json \
  --entry PLAN-B.json EXECUTION-B.json \
  --json \
  --output "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json"

./bin/work-plan render-digest \
  "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.json" \
  --output "$AUDIT_DIR/SUBAGENT_EXECUTION_DIGEST.md"
```

`digest` 会重新读取当前 Agent TOML 和 `config.toml`，用于发现配置漂移。历史 JSON Digest 已经通过校验后，只需补生成或重建 Markdown 时必须使用 `render-digest`；不得再次运行 `digest` 规避或覆盖历史配置证据。

最终回复中的“子任务执行概览”直接复用 `SUBAGENT_EXECUTION_DIGEST.md` 的 renderer 输出。不得根据 JSON 手工重建、合并或重排表头和统计列。固定表头为：

```markdown
| `agent_type` | 模型（Agent TOML） | 推理档位 | 执行尝试 | 验收通过 | 独立复核 |
|---|---|---|---:|---:|---:|
```

## 配置检查

```bash
./bin/work-plan \
  --agents-dir "$HOME/.codex/agents" \
  --codex-config "$HOME/.codex/config.toml" \
  doctor
```

Doctor 校验固定 Profile 和推荐的 `multi_agent_v2` 文件配置，但不能证明当前会话已重新加载。

## macOS 安装或原位替换

解压后在新目录中执行：

```bash
cd /path/to/multi-agent-orchestration-1.6.0
./scripts/install_macos.sh \
  --target /Users/sc/.codex/skills/multi-agent-orchestration
```

脚本会：

1. 在源目录运行编译、测试和示例漂移检查；
2. 备份现有目标目录；
3. 使用 `rsync --delete` 同步新版本；
4. 保留目标目录已有的 `.git/` 和 `.venv/`；
5. 初始化权限为 `0700` 的持久化 Audit Root；
6. 运行目标目录的 `doctor`。

不运行 Doctor：

```bash
./scripts/install_macos.sh \
  --target /Users/sc/.codex/skills/multi-agent-orchestration \
  --skip-doctor
```

## 更新 GitHub

若目标目录本身是现有 Git checkout，安装脚本会保留 `.git/`：

```bash
cd /Users/sc/.codex/skills/multi-agent-orchestration
git status --short
git diff --stat
git add .
git commit -m "feat: add persistent multi-agent audit bundles"
git push origin main
```

推送前建议再次运行：

```bash
./bin/verify-skill
```

## CLI

```bash
./bin/work-plan --help
```

支持：

- `plan`
- `validate`
- `summary`
- `digest`
- `render-digest`
- `guard-dispatch`
- `doctor`
- `audit-init` / `audit-import` / `audit-stage`
- `audit-finalize` / `audit-verify`
- `audit-list` / `audit-show` / `audit-delete` / `audit-prune`

完整字段与示例见 `references/work-plan.md`。
