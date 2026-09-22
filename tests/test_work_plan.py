from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
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
        report = work_plan.build_doctor_report(self.roles, self.config)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["errors"], [])

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
            subprocess.run(
                command
                + [
                    "digest",
                    "--entry",
                    str(output),
                    str(ROOT / "examples" / "work-plan.execution.json"),
                    "--json",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            dispatch_path = Path(directory) / "dispatch.json"
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
            subprocess.run(
                command
                + [
                    "guard-dispatch",
                    str(output),
                    "auth-cause-a1",
                    str(dispatch_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(command + ["doctor", "--json"], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
