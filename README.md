# multi-agent-orchestration

面向 Codex 固定角色 Profile 的多子代理编排 Skill，以及本地确定性的 WorkPlan Planner、Validator、Execution Summary、Digest 和 Doctor。

## 1.5.1 重点

- 固定角色模型、推理档位和 sandbox，不允许 spawn 时临时覆盖；
- fresh Worker 强制 `fork_turns: "none"`，并提供可由 hook 调用的 `guard-dispatch` 门禁；
- 默认派发调查、实现、验证、修正和交付的完整任务闭环；
- 普通进度不汇报，使用完成通知和长有界等待；
- 同一工作流延续优先复用合格 Worker；独立复核和 retry 使用 fresh Worker；
- 复用仍有效的测试证据，不重复整轮验证；
- Planner 读取 Codex 配置并约束实际并发容量；
- execution record 校验实际派发参数、写入路径和通信统计；
- 修复 retry 可能 supersede 错误 Worker 的交叉身份漏洞；
- 示例与 CLI 由端到端测试保持同步；
- 验证、示例再生成和 WorkPlan CLI 统一使用 Skill 专属 Python，不依赖 macOS 系统 Python。

## 目录职责

- `SKILL.md`：委派、路由、完整任务合同、生命周期、通信和验收流程；
- `scripts/work_plan.py`：确定性计划、配置、身份、执行证据和摘要校验；
- `references/work-plan.md`：完整协议；
- `examples/`：可实际运行的 schema 5/6 示例；
- `tests/`：单元与端到端测试；
- `scripts/install_macos.sh`：保留目标 `.git` 的 macOS 原位替换脚本。

## 当前版本

```text
Planner                 1.5.1
WorkPlan schema         5
Execution record        6
Summary schema          3
Digest schema           3
Doctor report           1
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
cd /path/to/multi-agent-orchestration-1.5.1
./scripts/install_macos.sh \
  --target /Users/sc/.codex/skills/multi-agent-orchestration
```

脚本会：

1. 在源目录运行编译、测试和示例漂移检查；
2. 备份现有目标目录；
3. 使用 `rsync --delete` 同步新版本；
4. 保留目标目录已有的 `.git/` 和 `.venv/`；
5. 运行目标目录的 `doctor`。

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
git commit -m "fix: use the dedicated Python runtime for local verification"
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
- `guard-dispatch`
- `doctor`

完整字段与示例见 `references/work-plan.md`。
