#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "work_plan.py"
spec = importlib.util.spec_from_file_location("work_plan", MODULE_PATH)
assert spec and spec.loader
work_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work_plan)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    examples = ROOT / "examples"
    roles = work_plan.load_roles(examples / "agents")
    config = work_plan.load_codex_config(examples / "config.toml", required=True)

    entries: list[tuple[dict, dict]] = []
    for stem in ("work-plan", "role-mismatch-retry"):
        draft = read_json(examples / f"{stem}.draft.json")
        plan = work_plan.canonical_plan(draft, roles, config)
        work_plan.validate_generated_plan(plan, roles, config)
        execution = read_json(examples / f"{stem}.execution.json")
        summary = work_plan.build_execution_summary(plan, execution, roles, config)
        write_json(examples / f"{stem}.generated.json", plan)
        write_json(examples / f"{stem}.execution-summary.json", summary)
        (examples / f"{stem}.execution-summary.txt").write_text(
            work_plan.render_execution_summary(summary), encoding="utf-8"
        )
        entries.append((plan, execution))

    digest = work_plan.build_execution_digest(entries, roles, config)
    write_json(examples / "subagent-execution-digest.json", digest)
    (examples / "subagent-execution-digest.md").write_text(
        work_plan.render_execution_digest(digest), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
