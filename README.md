# multi-agent-orchestration

用于 Codex 多子代理编排的 Skill，以及本地确定性的 WorkPlan
Planner、Validator、Execution Summary 和 Digest 工具。

## 职责边界

- `SKILL.md`：委派判定、角色路由、派发合同、生命周期与复核流程。
- `scripts/work_plan.py`：依赖、容量、波次、所有权冲突、Worker 身份、
  执行证据、Summary 和 Digest 的机械校验。
- `~/.codex/agents/*.toml`：模型、推理档位、sandbox 和角色专属行为。

Planner 不创建 Worker、不调用模型、不联网，也不选择或覆盖模型配置。

## 当前版本

- Planner：1.4.2
- WorkPlan schema：4
- Execution record：5
- Summary schema：2
- Digest schema：2

## Python

支持 Python 3.10 及以上。

- Python 3.11+ 使用标准库 `tomllib`。
- Python 3.10 根据 `requirements.txt` 使用 `tomli`。

推荐使用 Skill 专用环境：

```bash
~/.local/share/multi-agent-orchestration/.venv/bin/python
````

## 验证

```bash
cd ~/.codex/skills/multi-agent-orchestration

SKILL_PYTHON="$HOME/.local/share/multi-agent-orchestration/.venv/bin/python"

"$SKILL_PYTHON" -m compileall -q scripts tests
"$SKILL_PYTHON" -m unittest tests.test_work_plan
```

当前基线为 46 项测试通过。

## CLI

```bash
./bin/work-plan --help
```

支持：

* `plan`
* `validate`
* `summary`
* `digest`

## 安装约定

活动目录：

```text
~/.codex/skills/multi-agent-orchestration
```

Agent 配置目录：

```text
~/.codex/agents
```

修改 Skill 或 Agent 配置后，应使用新的 Codex 会话验证实际加载行为。
