from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "work_plan.py"
spec = importlib.util.spec_from_file_location("work_plan", SCRIPT)
assert spec and spec.loader
work_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work_plan)

ROLE_POLICIES = {
    "default": ("read-only", "gpt-5.6-luna", "high"),
    "analyst": ("read-only", "gpt-5.6-sol", "high"),
    "reviewer": ("read-only", "gpt-5.6-sol", "high"),
    "critical_reviewer": ("read-only", "gpt-6-astra", "medium"),
    "implementer": ("workspace-write", "gpt-5.6-sol", "high"),
    "debugger": ("workspace-write", "gpt-5.6-sol", "high"),
}


def task(
    task_id: str,
    role: str,
    *,
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    depends_on: list[str] | None = None,
    attempt: int = 1,
    replaces: str | None = None,
    independent_review: bool = False,
    review_of: list[str] | None = None,
    reuse_worker_id: str | None = None,
    task_name: str | None = None,
) -> dict:
    reuse = reuse_worker_id is not None
    return {
        "task_id": task_id,
        "task_name": task_name or task_id,
        "agent_type": role,
        "attempt": attempt,
        "replaces_task_id": replaces,
        "depends_on": depends_on or [],
        "read_paths": read_paths or [],
        "write_paths": write_paths or [],
        "independent_review": independent_review,
        "review_of_task_ids": review_of or [],
        "reuse_worker_id": reuse_worker_id,
        "accounting_scope": "current_attempt_delta" if reuse else "not_available",
        "dispatch_method": "followup_task" if reuse else "spawn_agent",
        "fork_turns": None if reuse else "none",
        "delivery_mode": "complete_task",
        "progress_policy": "blocker_or_final",
        "deliverable": f"deliver {task_id}",
        "acceptance_criteria": ["observable result"],
    }


def draft(
    tasks: list[dict],
    *,
    runtime_workers: list[dict] | None = None,
    prior_tasks: list[dict] | None = None,
    max_workers: int = 3,
    plan_id: str = "test-plan",
) -> dict:
    return {
        "version": 5,
        "plan_id": plan_id,
        "max_concurrent_workers": max_workers,
        "runtime_workers": runtime_workers or [],
        "prior_tasks": prior_tasks or [],
        "tasks": tasks,
    }


def runtime_worker(
    worker_id: str,
    task_id: str,
    role: str,
    *,
    status: str = "completed",
    runtime_ref: str | None = None,
    retired: bool = False,
    retirement_source: str = "unknown",
    superseded_by: str | None = None,
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
) -> dict:
    return {
        "worker_id": worker_id,
        "runtime_ref": runtime_ref,
        "runtime_ref_source": "spawn_metadata" if runtime_ref else "unknown",
        "task_id": task_id,
        "agent_type": role,
        "status": status,
        "retired_from_followup": retired,
        "retirement_source": retirement_source,
        "superseded_by_task_id": superseded_by,
        "read_paths": read_paths or (["src"] if not write_paths else []),
        "write_paths": write_paths or [],
    }


def execution_worker(
    task_row: dict,
    worker_id: str,
    *,
    runtime_ref: str | None = None,
    final_status: str = "completed",
    task_outcome: str = "accepted",
    observed_write_paths: list[str] | None = None,
    parent_followup_count: int = 0,
    intermediate_count: int = 0,
) -> dict:
    return {
        "task_id": task_row["task_id"],
        "agent_type": task_row["agent_type"],
        "worker_id": worker_id,
        "runtime_ref": runtime_ref,
        "runtime_ref_source": "thread_status_metadata" if runtime_ref else "unknown",
        "final_status": final_status,
        "task_outcome": task_outcome,
        "active_after_close": final_status in work_plan.OPEN_WORKER_STATES,
        "retired_from_followup": final_status in work_plan.TERMINAL_RETIREMENT_STATES,
        "retirement_source": (
            "runtime_terminal_status"
            if final_status in work_plan.TERMINAL_RETIREMENT_STATES
            else "unknown"
        ),
        "observed_dispatch": {
            "method": task_row["dispatch_method"],
            "task_name": task_row["task_name"],
            "fork_turns": task_row["fork_turns"],
            "model_override": None,
            "reasoning_effort_override": None,
        },
        "observed_write_paths": observed_write_paths,
        "parent_followup_count": parent_followup_count,
        "worker_intermediate_message_count": intermediate_count,
    }


def execution_record(
    plan: dict,
    workers: list[dict],
    *,
    active_worker_ids: list[str] | None = None,
    wait_calls: int = 1,
    wait_timeouts: int = 0,
    status_polls: int = 0,
) -> dict:
    observed = [row["observed_write_paths"] for row in workers]
    if any(value for value in observed if isinstance(value, list)):
        writes: bool | None = True
    elif any(value is None for value in observed):
        writes = None
    else:
        writes = False
    return {
        "version": 6,
        "plan_id": plan["plan_id"],
        "plan_command": ["./bin/work-plan", "plan", "/tmp/draft.json"],
        "validate_command": ["./bin/work-plan", "validate", "/tmp/plan.json"],
        "validate_status": "passed",
        "workers": workers,
        "active_worker_ids_after_execution": active_worker_ids or [],
        "writes_observed": writes,
        "communication": {
            "wait_call_count": wait_calls,
            "wait_timeout_count": wait_timeouts,
            "status_poll_count": status_polls,
        },
    }


class WorkPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agents = self.root / "agents"
        self.agents.mkdir()
        for name, (sandbox, model, effort) in ROLE_POLICIES.items():
            (self.agents / f"{name}.toml").write_text(
                f'name = "{name}"\n'
                f'description = "{name} description"\n'
                f'model = "{model}"\n'
                f'model_reasoning_effort = "{effort}"\n'
                f'sandbox_mode = "{sandbox}"\n'
                'developer_instructions = """Complete the assigned task. Report only blockers or final output."""\n',
                encoding="utf-8",
            )
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            """
[agents]
max_concurrent_threads_per_session = 4
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "high"

[features.multi_agent_v2]
enabled = true
hide_spawn_agent_metadata = false
expose_spawn_agent_model_overrides = false
tool_namespace = "agents"
wait_agent_enabled = true
non_code_mode_only = true
min_wait_timeout_ms = 50000
default_wait_timeout_ms = 120000
max_wait_timeout_ms = 240000
""".strip()
            + "\n",
            encoding="utf-8",
        )
        self.roles = work_plan.load_roles(self.agents)
        self.config = work_plan.load_codex_config(self.config_path, required=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(self, payload: dict) -> dict:
        return work_plan.canonical_plan(payload, self.roles, self.config)

    def test_roles_require_complete_fixed_profile(self) -> None:
        profile = self.roles["critical_reviewer"]
        self.assertEqual(profile["model"], "gpt-6-astra")
        self.assertEqual(profile["model_reasoning_effort"], "medium")
        self.assertEqual(profile["sandbox_mode"], "read-only")
        self.assertEqual(len(profile["config_sha256"]), 64)

        broken = self.root / "broken"
        broken.mkdir()
        (broken / "reviewer.toml").write_text(
            'name="reviewer"\nmodel="x"\nmodel_reasoning_effort="high"\nsandbox_mode="read-only"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(work_plan.PlanError, "description is required"):
            work_plan.load_roles(broken)

    def test_capacity_is_capped_by_codex_config(self) -> None:
        payload = draft(
            [task(f"scan-{i:02d}", "analyst", read_paths=[f"src/{i}"]) for i in range(6)],
            max_workers=8,
        )
        result = self.plan(payload)
        self.assertEqual(result["effective_capacity"], 4)
        self.assertEqual(len(result["waves"][0]["task_ids"]), 4)
        self.assertEqual(result["codex_config_evidence"]["capacity_source"], "codex_config")

    def test_doctor_accepts_fixed_profile_configuration(self) -> None:
        audit_root = self.root / "audits"
        audit_root.mkdir()
        report = work_plan.build_doctor_report(self.roles, self.config, audit_root)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["report_version"], 2)
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["audit_root"]["exists"])
        self.assertTrue(report["audit_root"]["writable"])

    def test_fresh_task_requires_isolated_fork(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        row["fork_turns"] = "all"
        with self.assertRaisesRegex(work_plan.PlanError, "fork_turns=none"):
            self.plan(draft([row]))

    def test_fresh_task_rejects_followup_dispatch(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        row["dispatch_method"] = "followup_task"
        with self.assertRaisesRegex(work_plan.PlanError, "dispatch_method=spawn_agent"):
            self.plan(draft([row]))

    def test_reuse_requires_followup_and_delta_accounting(self) -> None:
        runtime = [runtime_worker("worker-analysis", "old-analysis-a1", "analyst")]
        row = task(
            "scan-a1",
            "analyst",
            read_paths=["src"],
            reuse_worker_id="worker-analysis",
        )
        result = self.plan(draft([row], runtime_workers=runtime))
        self.assertEqual(result["ready_task_ids"], ["scan-a1"])
        self.assertEqual(result["tasks"][0]["dispatch_method"], "followup_task")

        row["accounting_scope"] = "not_available"
        with self.assertRaisesRegex(work_plan.PlanError, "current_attempt_delta"):
            self.plan(draft([row], runtime_workers=runtime))

    def test_same_worker_cannot_be_reused_twice_in_one_plan(self) -> None:
        runtime = [runtime_worker("worker-analysis", "old-analysis-a1", "analyst")]
        rows = [
            task("scan-a1", "analyst", read_paths=["src/a"], reuse_worker_id="worker-analysis"),
            task("scan-b1", "analyst", read_paths=["src/b"], reuse_worker_id="worker-analysis"),
        ]
        with self.assertRaisesRegex(work_plan.PlanError, "at most one task"):
            self.plan(draft(rows, runtime_workers=runtime))

    def test_completed_runtime_does_not_resolve_dependency(self) -> None:
        runtime = [runtime_worker("worker-upstream", "upstream-a1", "analyst")]
        row = task(
            "downstream-a1",
            "analyst",
            read_paths=["src/downstream"],
            depends_on=["upstream-a1"],
        )
        result = self.plan(draft([row], runtime_workers=runtime))
        self.assertEqual(result["ready_task_ids"], [])
        self.assertIn("no accepted task outcome", result["blocked_reasons"]["downstream-a1"][0])

    def test_prior_accepted_resolves_external_dependency(self) -> None:
        runtime = [runtime_worker("worker-upstream", "upstream-a1", "analyst")]
        prior = [
            {"task_id": "upstream-a1", "attempt": 1, "status": "accepted", "worker_id": "worker-upstream"}
        ]
        row = task(
            "downstream-a1",
            "analyst",
            read_paths=["src/downstream"],
            depends_on=["upstream-a1"],
        )
        result = self.plan(draft([row], runtime_workers=runtime, prior_tasks=prior))
        self.assertEqual(result["ready_task_ids"], ["downstream-a1"])

    def test_write_conflict_creates_separate_waves(self) -> None:
        rows = [
            task("write-a1", "implementer", write_paths=["src/shared.py"]),
            task("write-b1", "debugger", write_paths=["src/shared.py"]),
        ]
        result = self.plan(draft(rows))
        self.assertEqual(len(result["waves"]), 2)

    def test_active_worker_blocks_conflicting_task_and_reduces_capacity(self) -> None:
        runtime = [
            runtime_worker(
                "worker-live",
                "live-task-a1",
                "implementer",
                status="running",
                read_paths=["src/live"],
                write_paths=["src/live/state.py"],
            )
        ]
        rows = [
            task("safe-a1", "analyst", read_paths=["src/other"]),
            task("conflict-a1", "analyst", read_paths=["src/live"]),
        ]
        result = self.plan(draft(rows, runtime_workers=runtime, max_workers=2))
        self.assertEqual(result["available_slots"], 1)
        self.assertEqual(result["ready_task_ids"], ["safe-a1"])
        self.assertEqual(result["blocked_task_ids"], ["conflict-a1"])

    def test_retry_rejects_prior_worker_bound_to_another_task(self) -> None:
        runtime = [runtime_worker("worker-wrong", "unrelated-a1", "analyst")]
        prior = [
            {"task_id": "old-task-a1", "attempt": 1, "status": "role_mismatch", "worker_id": "worker-wrong"}
        ]
        row = task(
            "new-task-a2",
            "analyst",
            read_paths=["src"],
            attempt=2,
            replaces="old-task-a1",
        )
        with self.assertRaisesRegex(work_plan.PlanError, "belongs to task unrelated-a1"):
            self.plan(draft([row], runtime_workers=runtime, prior_tasks=prior))

    def test_retry_supersedes_inactive_worker(self) -> None:
        runtime = [runtime_worker("worker-old", "old-task-a1", "reviewer")]
        prior = [
            {"task_id": "old-task-a1", "attempt": 1, "status": "role_mismatch", "worker_id": "worker-old"}
        ]
        row = task(
            "new-task-a2",
            "analyst",
            read_paths=["src"],
            attempt=2,
            replaces="old-task-a1",
        )
        result = self.plan(draft([row], runtime_workers=runtime, prior_tasks=prior))
        self.assertEqual(result["superseded_worker_ids"], ["worker-old"])
        self.assertEqual(result["runtime_workers"][0]["superseded_by_task_id"], "new-task-a2")

    def test_independent_review_must_be_fresh(self) -> None:
        runtime = [runtime_worker("worker-review", "old-review-a1", "reviewer")]
        prior = [
            {"task_id": "impl-target-a1", "attempt": 1, "status": "accepted", "worker_id": None}
        ]
        row = task(
            "review-new-a1",
            "reviewer",
            read_paths=["src"],
            independent_review=True,
            review_of=["impl-target-a1"],
            reuse_worker_id="worker-review",
        )
        with self.assertRaisesRegex(work_plan.PlanError, "independent review must use a fresh Worker"):
            self.plan(draft([row], runtime_workers=runtime, prior_tasks=prior))

    def test_generated_plan_detects_manual_edit(self) -> None:
        generated = self.plan(draft([task("scan-a1", "analyst", read_paths=["src"])]))
        work_plan.validate_generated_plan(generated, self.roles, self.config)
        tampered = json.loads(json.dumps(generated))
        tampered["tasks"][0]["assigned_wave"] = 2
        with self.assertRaisesRegex(work_plan.PlanError, "tasks"):
            work_plan.validate_generated_plan(tampered, self.roles, self.config)

    def test_guard_dispatch_accepts_planned_isolated_spawn(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row]))
        dispatch = {
            "method": "spawn_agent",
            "task_name": "scan-a1",
            "agent_type": "analyst",
            "fork_turns": "none",
            "model": None,
            "reasoning_effort": None,
        }
        result = work_plan.guard_dispatch(
            plan, "scan-a1", dispatch, self.roles, self.config
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observed_dispatch"]["fork_turns"], "none")

    def test_guard_audit_membership_requires_same_open_stage(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row]))
        audit_root = self.root / "audits"
        bundle, _ = work_plan.create_audit_bundle(
            audit_root, ROOT, "guard membership"
        )
        stage = work_plan.allocate_audit_stage(bundle, "analysis")
        plan_path = Path(stage["paths"]["plan"])
        dispatch_path = (
            Path(stage["paths"]["dispatch_dir"]) / "scan-a1.json"
        )
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        dispatch_path.write_text(
            json.dumps(
                {
                    "method": "spawn_agent",
                    "task_name": "scan-a1",
                    "agent_type": "analyst",
                    "fork_turns": "none",
                    "model": None,
                    "reasoning_effort": None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        membership = work_plan.guard_audit_membership(
            bundle, plan_path, dispatch_path
        )
        self.assertEqual(membership["audit_id"], bundle.name)
        self.assertEqual(membership["audit_stage"], "01-analysis")

        outside = self.root / "outside-dispatch.json"
        shutil.copy2(dispatch_path, outside)
        with self.assertRaisesRegex(work_plan.PlanError, "inside the persistent"):
            work_plan.guard_audit_membership(bundle, plan_path, outside)

        other_stage = work_plan.allocate_audit_stage(bundle, "other")
        other_dispatch = Path(other_stage["paths"]["dispatch_dir"]) / "scan-a1.json"
        shutil.copy2(dispatch_path, other_dispatch)
        with self.assertRaisesRegex(work_plan.PlanError, "same audit stage"):
            work_plan.guard_audit_membership(bundle, plan_path, other_dispatch)

    def test_guard_dispatch_rejects_full_history_and_overrides(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row]))
        dispatch = {
            "method": "spawn_agent",
            "task_name": "scan-a1",
            "agent_type": "analyst",
            "fork_turns": "all",
            "model": None,
            "reasoning_effort": None,
        }
        with self.assertRaisesRegex(work_plan.PlanError, "fork_turns"):
            work_plan.guard_dispatch(plan, "scan-a1", dispatch, self.roles, self.config)
        dispatch["fork_turns"] = "none"
        dispatch["model"] = "gpt-6-astra"
        with self.assertRaisesRegex(work_plan.PlanError, "model override"):
            work_plan.guard_dispatch(plan, "scan-a1", dispatch, self.roles, self.config)

    def test_execution_rejects_model_override(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row]))
        worker = execution_worker(plan["tasks"][0], "worker-scan", observed_write_paths=[])
        worker["observed_dispatch"]["model_override"] = "gpt-6-astra"
        execution = execution_record(plan, [worker])
        with self.assertRaisesRegex(work_plan.PlanError, "model_override must be null"):
            work_plan.build_execution_summary(plan, execution, self.roles, self.config)

    def test_execution_rejects_nonisolated_fresh_spawn(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row]))
        worker = execution_worker(plan["tasks"][0], "worker-scan", observed_write_paths=[])
        worker["observed_dispatch"]["fork_turns"] = "all"
        execution = execution_record(plan, [worker])
        with self.assertRaisesRegex(work_plan.PlanError, "does not match WorkPlan"):
            work_plan.build_execution_summary(plan, execution, self.roles, self.config)

    def test_execution_rejects_write_outside_ownership(self) -> None:
        row = task("impl-a1", "implementer", write_paths=["src/owned"])
        plan = self.plan(draft([row]))
        worker = execution_worker(
            plan["tasks"][0], "worker-impl", observed_write_paths=["src/other/file.py"]
        )
        execution = execution_record(plan, [worker])
        with self.assertRaisesRegex(work_plan.PlanError, "outside ownership"):
            work_plan.build_execution_summary(plan, execution, self.roles, self.config)

    def test_unknown_write_paths_are_preserved_as_anomaly(self) -> None:
        row = task("impl-a1", "implementer", write_paths=["src/owned"])
        plan = self.plan(draft([row], plan_id="unknown-write-plan"))
        worker = execution_worker(plan["tasks"][0], "worker-impl", observed_write_paths=None)
        execution = execution_record(plan, [worker])
        summary = work_plan.build_execution_summary(plan, execution, self.roles, self.config)
        self.assertEqual(summary["unknown_write_path_task_ids"], ["impl-a1"])
        digest = work_plan.build_execution_digest([(plan, execution)], self.roles, self.config)
        self.assertEqual(digest["anomalies"][0]["type"], "unknown_write_evidence")

    def test_writes_observed_must_match_per_worker_evidence(self) -> None:
        row = task("impl-a1", "implementer", write_paths=["src/owned"])
        plan = self.plan(draft([row]))
        worker = execution_worker(
            plan["tasks"][0], "worker-impl", observed_write_paths=["src/owned/file.py"]
        )
        execution = execution_record(plan, [worker])
        execution["writes_observed"] = False
        with self.assertRaisesRegex(work_plan.PlanError, "inconsistent"):
            work_plan.build_execution_summary(plan, execution, self.roles, self.config)

    def test_fresh_runtime_ref_cannot_match_existing_worker(self) -> None:
        runtime = [
            runtime_worker(
                "worker-old",
                "old-task-a1",
                "analyst",
                runtime_ref="/root/existing",
            )
        ]
        row = task("scan-a1", "analyst", read_paths=["src/next"])
        plan = self.plan(draft([row], runtime_workers=runtime))
        worker = execution_worker(
            plan["tasks"][0], "worker-new", runtime_ref="/root/existing", observed_write_paths=[]
        )
        execution = execution_record(plan, [worker])
        with self.assertRaisesRegex(work_plan.PlanError, "fresh runtime_ref"):
            work_plan.build_execution_summary(plan, execution, self.roles, self.config)

    def test_reused_worker_runtime_identity_must_match(self) -> None:
        runtime = [
            runtime_worker(
                "worker-analysis",
                "old-analysis-a1",
                "analyst",
                runtime_ref="/root/analysis",
            )
        ]
        row = task(
            "scan-a1",
            "analyst",
            read_paths=["src"],
            reuse_worker_id="worker-analysis",
        )
        plan = self.plan(draft([row], runtime_workers=runtime))
        worker = execution_worker(
            plan["tasks"][0],
            "worker-analysis",
            runtime_ref="/root/wrong",
            observed_write_paths=[],
        )
        execution = execution_record(plan, [worker])
        with self.assertRaisesRegex(work_plan.PlanError, "runtime_ref does not match"):
            work_plan.build_execution_summary(plan, execution, self.roles, self.config)

    def test_completed_role_mismatch_is_not_accepted(self) -> None:
        first_row = task("review-a1", "reviewer", read_paths=["src"])
        first_plan = self.plan(draft([first_row], plan_id="first-plan"))
        first_worker = execution_worker(
            first_plan["tasks"][0],
            "worker-review",
            task_outcome="role_mismatch",
            observed_write_paths=[],
        )
        first_execution = execution_record(first_plan, [first_worker])
        digest = work_plan.build_execution_digest(
            [(first_plan, first_execution)], self.roles, self.config
        )
        self.assertEqual(digest["accepted_attempt_count"], 0)
        self.assertEqual(digest["runtime_status_counts"], {"completed": 1})
        self.assertEqual(digest["task_outcome_counts"], {"role_mismatch": 1})

    def test_digest_aggregates_communication(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row], plan_id="communication-plan"))
        worker = execution_worker(
            plan["tasks"][0],
            "worker-scan",
            observed_write_paths=[],
            parent_followup_count=1,
            intermediate_count=2,
        )
        execution = execution_record(
            plan, [worker], wait_calls=3, wait_timeouts=2, status_polls=1
        )
        digest = work_plan.build_execution_digest([(plan, execution)], self.roles, self.config)
        stats = digest["dispatch_and_communication"]
        self.assertEqual(stats["isolated_fork_count"], 1)
        self.assertEqual(stats["parent_followup_count"], 1)
        self.assertEqual(stats["worker_intermediate_message_count"], 2)
        self.assertEqual(stats["wait_call_count"], 3)
        self.assertEqual(stats["wait_timeout_count"], 2)

    def test_digest_markdown_table_contract_is_exact(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row], plan_id="markdown-contract-plan"))
        worker = execution_worker(plan["tasks"][0], "worker-scan", observed_write_paths=[])
        execution = execution_record(plan, [worker])
        digest = work_plan.build_execution_digest([(plan, execution)], self.roles, self.config)

        rendered = work_plan.render_execution_digest(digest)
        lines = rendered.splitlines()

        self.assertEqual(
            lines[:4],
            [
                "### 子任务执行概览",
                "",
                "| `agent_type` | 模型（Agent TOML） | 推理档位 | 执行尝试 | 验收通过 | 独立复核 |",
                "|---|---|---|---:|---:|---:|",
            ],
        )
        self.assertIn(
            "| `analyst` | `gpt-5.6-sol` | `high` | 1 | 1 | 0 |",
            lines,
        )
        self.assertNotIn("Agent 类型Agent TOML 配置执行尝试验收通过独立复核", rendered)

    def test_render_digest_snapshot_survives_live_config_drift(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row], plan_id="render-snapshot-plan"))
        worker = execution_worker(plan["tasks"][0], "worker-scan", observed_write_paths=[])
        execution = execution_record(plan, [worker])
        digest = work_plan.build_execution_digest([(plan, execution)], self.roles, self.config)

        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8") + "# later config edit\n",
            encoding="utf-8",
        )
        drifted_config = work_plan.load_codex_config(self.config_path, required=True)
        with self.assertRaisesRegex(
            work_plan.PlanError,
            "codex_config_evidence",
        ):
            work_plan.build_execution_digest([(plan, execution)], self.roles, drifted_config)

        snapshot = work_plan.validate_digest_snapshot_for_render(digest)
        rendered = work_plan.render_execution_digest(snapshot)
        self.assertIn(
            "| `analyst` | `gpt-5.6-sol` | `high` | 1 | 1 | 0 |",
            rendered,
        )

    def test_render_digest_snapshot_rejects_wrong_version(self) -> None:
        row = task("scan-a1", "analyst", read_paths=["src"])
        plan = self.plan(draft([row], plan_id="render-version-plan"))
        worker = execution_worker(plan["tasks"][0], "worker-scan", observed_write_paths=[])
        execution = execution_record(plan, [worker])
        digest = work_plan.build_execution_digest([(plan, execution)], self.roles, self.config)
        digest["digest_version"] = 999
        with self.assertRaisesRegex(work_plan.PlanError, "digest_version"):
            work_plan.validate_digest_snapshot_for_render(digest)

    def test_digest_uses_latest_active_snapshot(self) -> None:
        row1 = task("scan-a1", "analyst", read_paths=["src/a"])
        plan1 = self.plan(draft([row1], plan_id="active-first"))
        execution1 = execution_record(
            plan1, [], active_worker_ids=["worker-external"], wait_calls=1
        )
        row2 = task("scan-b1", "analyst", read_paths=["src/b"])
        plan2 = self.plan(draft([row2], plan_id="active-second"))
        worker2 = execution_worker(plan2["tasks"][0], "worker-b", observed_write_paths=[])
        execution2 = execution_record(plan2, [worker2], active_worker_ids=[])
        digest = work_plan.build_execution_digest(
            [(plan1, execution1), (plan2, execution2)], self.roles, self.config
        )
        self.assertEqual(digest["active_worker_ids_after_execution"], [])

    def test_skill_python_respects_explicit_runtime(self) -> None:
        env = os.environ.copy()
        env["MULTI_AGENT_ORCHESTRATION_PYTHON"] = sys.executable
        result = subprocess.run(
            [str(ROOT / "bin" / "skill-python"), "--print-path"],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(
            Path(result.stdout.strip()).resolve(),
            Path(sys.executable).resolve(),
        )

    def test_examples_regenerate_without_diff(self) -> None:
        before = {
            path.relative_to(ROOT): path.read_bytes()
            for path in (ROOT / "examples").glob("*.generated.json")
        }
        subprocess.run([sys.executable, str(ROOT / "scripts" / "regenerate_examples.py")], check=True)
        after = {
            path.relative_to(ROOT): path.read_bytes()
            for path in (ROOT / "examples").glob("*.generated.json")
        }
        self.assertEqual(before, after)


    def _populate_complete_audit_bundle(self, *, risk: str = "normal") -> Path:
        audit_root = self.root / "audits"
        bundle, _ = work_plan.create_audit_bundle(
            audit_root,
            ROOT,
            "示例审计任务",
            risk=risk,
            retention_days=1,
            now=datetime(2026, 9, 22, tzinfo=timezone.utc),
        )
        stage_a = work_plan.allocate_audit_stage(bundle, "analysis")
        stage_b = work_plan.allocate_audit_stage(bundle, "retry")

        def copy(source: Path, destination: str) -> None:
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        copy(ROOT / "examples" / "work-plan.draft.json", stage_a["paths"]["draft"])
        copy(ROOT / "examples" / "work-plan.generated.json", stage_a["paths"]["plan"])
        copy(ROOT / "examples" / "work-plan.execution.json", stage_a["paths"]["execution"])
        copy(
            ROOT / "examples" / "work-plan.execution-summary.json",
            stage_a["paths"]["summary_json"],
        )
        copy(
            ROOT / "examples" / "work-plan.dispatch.json",
            str(Path(stage_a["paths"]["dispatch_dir"]) / "auth-cause-a1.json"),
        )

        copy(
            ROOT / "examples" / "role-mismatch-retry.draft.json",
            stage_b["paths"]["draft"],
        )
        copy(
            ROOT / "examples" / "role-mismatch-retry.generated.json",
            stage_b["paths"]["plan"],
        )
        copy(
            ROOT / "examples" / "role-mismatch-retry.execution.json",
            stage_b["paths"]["execution"],
        )
        copy(
            ROOT / "examples" / "role-mismatch-retry.execution-summary.json",
            stage_b["paths"]["summary_json"],
        )
        dispatch_b = {
            "method": "spawn_agent",
            "task_name": "分析价格计算链",
            "agent_type": "analyst",
            "fork_turns": "none",
            "model": None,
            "reasoning_effort": None,
        }
        dispatch_b_path = Path(stage_b["paths"]["dispatch_dir"]) / "pricing-explain-a2.json"
        dispatch_b_path.write_text(
            json.dumps(dispatch_b, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        copy(
            ROOT / "examples" / "subagent-execution-digest.json",
            str(bundle / "SUBAGENT_EXECUTION_DIGEST.json"),
        )
        return bundle

    def test_audit_bundle_lifecycle_is_discoverable_and_verifiable(self) -> None:
        audit_root = self.root / "audits"
        bundle = self._populate_complete_audit_bundle()

        report = work_plan.finalize_audit_bundle(bundle)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["bundle_status"], "closed")
        self.assertTrue((bundle / "SUBAGENT_EXECUTION_DIGEST.md").is_file())
        self.assertTrue((bundle / "01-analysis.summary.txt").is_file())
        self.assertTrue((bundle / "02-retry.summary.txt").is_file())

        verify = work_plan.inspect_audit_bundle(bundle, compare_manifest=True)
        self.assertEqual(verify["status"], "passed")
        manifest = work_plan.show_audit_bundle(bundle)
        self.assertEqual(manifest["status"], "closed")
        self.assertEqual(manifest["bundle_version"], 1)
        self.assertEqual(manifest["summary"]["plan_count"], 2)
        self.assertEqual(len(manifest["artifacts"]), 14)

        listed = work_plan.list_audit_bundles(audit_root)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["audit_id"], manifest["audit_id"])
        self.assertEqual(
            work_plan.resolve_audit_bundle(manifest["audit_id"], audit_root),
            bundle,
        )


    def test_audit_import_migrates_existing_temporary_directory(self) -> None:
        source = self._populate_complete_audit_bundle()
        import_root = self.root / "imported-audits"
        bundle, report = work_plan.import_audit_directory(
            import_root,
            source,
            ROOT,
            "imported smoke test",
            retention_days=7,
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(work_plan._read_audit_manifest(bundle)["status"], "closed")
        self.assertTrue((bundle / "SUBAGENT_EXECUTION_DIGEST.md").is_file())
        self.assertEqual(len(work_plan.list_audit_bundles(import_root)), 1)
        self.assertIn("audit bundle has no allocated stages", report["warnings"])

    def test_audit_verify_detects_artifact_tampering(self) -> None:
        bundle = self._populate_complete_audit_bundle()
        self.assertEqual(work_plan.finalize_audit_bundle(bundle)["status"], "passed")
        execution = bundle / "01-analysis.execution.json"
        execution.write_text(execution.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        report = work_plan.inspect_audit_bundle(bundle, compare_manifest=True)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any("checksum mismatch" in item for item in report["errors"]),
            report["errors"],
        )

    def test_audit_delete_requires_confirmation_and_protects_attention(self) -> None:
        audit_root = self.root / "audits"
        bundle = self._populate_complete_audit_bundle()
        self.assertEqual(work_plan.finalize_audit_bundle(bundle)["status"], "passed")
        with self.assertRaisesRegex(work_plan.PlanError, "requires --yes"):
            work_plan.delete_audit_bundle(bundle, confirmed=False, force=False)

        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["summary"]["attention_required"] = True
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(work_plan.PlanError, "with anomalies"):
            work_plan.delete_audit_bundle(bundle, confirmed=True, force=False)
        result = work_plan.delete_audit_bundle(bundle, confirmed=True, force=True)
        self.assertEqual(result["status"], "deleted")
        self.assertFalse(bundle.exists())
        self.assertEqual(work_plan.list_audit_bundles(audit_root), [])


    def test_invalid_manifest_remains_listable_and_force_deletable(self) -> None:
        audit_root = self.root / "audits"
        bundle, _ = work_plan.create_audit_bundle(audit_root, ROOT, "invalid manifest")
        (bundle / "manifest.json").write_text("{broken", encoding="utf-8")
        rows = work_plan.list_audit_bundles(audit_root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "invalid")
        resolved = work_plan.resolve_audit_bundle(
            bundle.name,
            audit_root,
            allow_invalid=True,
        )
        result = work_plan.delete_audit_bundle(
            resolved,
            confirmed=True,
            force=True,
        )
        self.assertEqual(result["status"], "deleted")
        self.assertFalse(bundle.exists())

    def test_audit_prune_is_dry_run_by_default(self) -> None:
        audit_root = self.root / "audits"
        bundle = self._populate_complete_audit_bundle()
        self.assertEqual(work_plan.finalize_audit_bundle(bundle)["status"], "passed")
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["retention"]["delete_after"] = "2026-09-20T00:00:00Z"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        current = datetime(2026, 9, 22, tzinfo=timezone.utc)
        dry_run = work_plan.prune_audit_bundles(
            audit_root,
            apply=False,
            include_attention=False,
            now=current,
        )
        self.assertEqual(dry_run["selected_count"], 1)
        self.assertTrue(bundle.exists())
        applied = work_plan.prune_audit_bundles(
            audit_root,
            apply=True,
            include_attention=False,
            now=current,
        )
        self.assertEqual(applied["selected_count"], 1)
        self.assertFalse(bundle.exists())

    def test_audit_finalize_rejects_incomplete_bundle(self) -> None:
        bundle, _ = work_plan.create_audit_bundle(
            self.root / "audits",
            ROOT,
            "incomplete",
        )
        work_plan.allocate_audit_stage(bundle, "implementation")
        report = work_plan.finalize_audit_bundle(bundle)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["errors"])
        manifest = work_plan._read_audit_manifest(bundle)
        self.assertEqual(manifest["status"], "open")
        self.assertEqual(manifest["verification"]["status"], "failed")


    def test_cli_audit_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_root = root / "audits"
            base = [
                sys.executable,
                str(SCRIPT),
                "--audit-root",
                str(audit_root),
            ]
            init_result = subprocess.run(
                base
                + [
                    "audit-init",
                    "--task-name",
                    "CLI audit lifecycle",
                    "--repo-root",
                    str(ROOT),
                    "--retention-days",
                    "1",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            descriptor = json.loads(init_result.stdout)
            bundle = Path(descriptor["audit_dir"])

            def stage(name: str) -> dict:
                result = subprocess.run(
                    base + ["audit-stage", str(bundle), "--name", name],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return json.loads(result.stdout)

            stage_a = stage("analysis")
            stage_b = stage("retry")

            def copy(source: Path, destination: str) -> None:
                target = Path(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            copy(ROOT / "examples" / "work-plan.draft.json", stage_a["paths"]["draft"])
            copy(ROOT / "examples" / "work-plan.generated.json", stage_a["paths"]["plan"])
            copy(ROOT / "examples" / "work-plan.execution.json", stage_a["paths"]["execution"])
            copy(
                ROOT / "examples" / "work-plan.execution-summary.json",
                stage_a["paths"]["summary_json"],
            )
            copy(
                ROOT / "examples" / "work-plan.dispatch.json",
                str(Path(stage_a["paths"]["dispatch_dir"]) / "auth-cause-a1.json"),
            )
            copy(
                ROOT / "examples" / "role-mismatch-retry.draft.json",
                stage_b["paths"]["draft"],
            )
            copy(
                ROOT / "examples" / "role-mismatch-retry.generated.json",
                stage_b["paths"]["plan"],
            )
            copy(
                ROOT / "examples" / "role-mismatch-retry.execution.json",
                stage_b["paths"]["execution"],
            )
            copy(
                ROOT / "examples" / "role-mismatch-retry.execution-summary.json",
                stage_b["paths"]["summary_json"],
            )
            dispatch_b = {
                "method": "spawn_agent",
                "task_name": "分析价格计算链",
                "agent_type": "analyst",
                "fork_turns": "none",
                "model": None,
                "reasoning_effort": None,
            }
            dispatch_b_path = Path(stage_b["paths"]["dispatch_dir"]) / "pricing-explain-a2.json"
            dispatch_b_path.write_text(
                json.dumps(dispatch_b, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            copy(
                ROOT / "examples" / "subagent-execution-digest.json",
                str(bundle / "SUBAGENT_EXECUTION_DIGEST.json"),
            )

            subprocess.run(base + ["audit-finalize", str(bundle)], check=True)
            subprocess.run(base + ["audit-verify", descriptor["audit_id"]], check=True)
            listed = subprocess.run(
                base + ["audit-list", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(listed.stdout)["count"], 1)
            shown = subprocess.run(
                base + ["audit-show", descriptor["audit_id"], "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(shown.stdout)["status"], "closed")
            subprocess.run(
                base + ["audit-delete", descriptor["audit_id"], "--yes"],
                check=True,
            )
            self.assertFalse(bundle.exists())

    def test_cli_plan_validate_summary_digest_and_doctor(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT),
            "--agents-dir",
            str(ROOT / "examples" / "agents"),
            "--codex-config",
            str(ROOT / "examples" / "config.toml"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            audit_root = Path(directory) / "audits"
            command = command[:2] + ["--audit-root", str(audit_root)] + command[2:]
            output = Path(directory) / "plan.json"
            subprocess.run(
                command
                + [
                    "plan",
                    str(ROOT / "examples" / "work-plan.draft.json"),
                    "--output",
                    str(output),
                ],
                check=True,
            )
            subprocess.run(command + ["validate", str(output)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(
                command
                + [
                    "summary",
                    str(output),
                    str(ROOT / "examples" / "work-plan.execution.json"),
                    "--json",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            digest_path = Path(directory) / "digest.json"
            subprocess.run(
                command
                + [
                    "digest",
                    "--entry",
                    str(output),
                    str(ROOT / "examples" / "work-plan.execution.json"),
                    "--json",
                    "--output",
                    str(digest_path),
                ],
                check=True,
            )
            rendered_path = Path(directory) / "digest.md"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--agents-dir",
                    str(Path(directory) / "missing-agents"),
                    "--codex-config",
                    str(Path(directory) / "missing-config.toml"),
                    "render-digest",
                    str(digest_path),
                    "--output",
                    str(rendered_path),
                ],
                check=True,
            )
            self.assertIn(
                "| `analyst` | `gpt-5.6-sol` | `high` | 1 | 1 | 0 |",
                rendered_path.read_text(encoding="utf-8"),
            )
            bundle, _ = work_plan.create_audit_bundle(
                audit_root, ROOT, "CLI guard"
            )
            stage = work_plan.allocate_audit_stage(bundle, "analysis")
            guarded_plan = Path(stage["paths"]["plan"])
            shutil.copy2(output, guarded_plan)
            dispatch_path = (
                Path(stage["paths"]["dispatch_dir"]) / "auth-cause-a1.json"
            )
            dispatch_path.write_text(
                json.dumps(
                    {
                        "method": "followup_task",
                        "task_name": "分析认证缓存根因",
                        "agent_type": "analyst",
                        "fork_turns": None,
                        "model": None,
                        "reasoning_effort": None,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            guarded = subprocess.run(
                command
                + [
                    "guard-dispatch",
                    str(guarded_plan),
                    "auth-cause-a1",
                    str(dispatch_path),
                    "--audit",
                    bundle.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(guarded.stdout)["audit_id"], bundle.name)
            subprocess.run(command + ["doctor", "--json"], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
