from __future__ import annotations

import builtins
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "work_plan.py"
spec = importlib.util.spec_from_file_location("work_plan", SCRIPT)
assert spec and spec.loader
work_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work_plan)


ROLE_POLICIES = {
    "default": "read-only",
    "analyst": "read-only",
    "reviewer": "read-only",
    "critical_reviewer": "read-only",
    "implementer": "workspace-write",
    "debugger": "workspace-write",
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
    accounting_scope: str = "not_available",
) -> dict:
    return {
        "task_id": task_id,
        "task_name": task_id,
        "agent_type": role,
        "attempt": attempt,
        "replaces_task_id": replaces,
        "depends_on": depends_on or [],
        "read_paths": read_paths or [],
        "write_paths": write_paths or [],
        "independent_review": independent_review,
        "review_of_task_ids": review_of or [],
        "reuse_worker_id": reuse_worker_id,
        "accounting_scope": accounting_scope,
        "deliverable": f"deliver {task_id}",
        "acceptance_criteria": ["observable result"],
    }


def draft(
    tasks: list[dict],
    *,
    runtime_workers=None,
    prior_tasks=None,
    max_workers=3,
    plan_id="test-plan",
) -> dict:
    return {
        "version": 4,
        "plan_id": plan_id,
        "max_concurrent_workers": max_workers,
        "runtime_workers": runtime_workers or [],
        "prior_tasks": prior_tasks or [],
        "tasks": tasks,
    }


def execution_record(
    plan_id: str,
    workers: list[dict],
    *,
    active_worker_ids: list[str] | None = None,
    writes_observed: bool | None = False,
) -> dict:
    for worker in workers:
        if "task_outcome" not in worker:
            final_status = worker.get("final_status")
            worker["task_outcome"] = {
                "pending": "not_evaluated",
                "running": "not_evaluated",
                "failed": "failed",
                "blocked": "blocked",
                "early_stopped": "early_stopped",
                "interrupted": "interrupted",
                "stopped": "interrupted",
                "reclaimed": "interrupted",
            }.get(final_status, "accepted")

    return {
        "version": 5,
        "plan_id": plan_id,
        "plan_command": ["./bin/work-plan", "plan", "/tmp/draft.json", "--output", "/tmp/plan.json"],
        "validate_command": ["./bin/work-plan", "validate", "/tmp/plan.json"],
        "validate_status": "passed",
        "workers": workers,
        "active_worker_ids_after_execution": active_worker_ids or [],
        "writes_observed": writes_observed,
    }


class WorkPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agents = Path(self.temp.name) / "agents"
        self.agents.mkdir()
        for name, sandbox in ROLE_POLICIES.items():
            (self.agents / f"{name}.toml").write_text(
                f'name = "{name}"\nmodel = "test"\nmodel_reasoning_effort = "high"\n'
                f'sandbox_mode = "{sandbox}"\n',
                encoding="utf-8",
            )
        self.roles = work_plan.load_roles(self.agents)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_role_profiles_include_model_reasoning_and_sandbox(self) -> None:
        profile = self.roles["critical_reviewer"]
        self.assertEqual(profile["model"], "test")
        self.assertEqual(profile["model_reasoning_effort"], "high")
        self.assertEqual(profile["sandbox_mode"], "read-only")
        self.assertEqual(profile["config_file"], "critical_reviewer.toml")

    def _parse_with_python310_tomli_fallback(self, path: Path) -> dict[str, str]:
        try:
            import tomllib as reference_toml
        except ImportError:  # pragma: no cover - exercised by the real 3.10 gate
            import tomli as reference_toml

        fake_tomli = types.SimpleNamespace(
            load=reference_toml.load,
            TOMLDecodeError=reference_toml.TOMLDecodeError,
        )
        real_import = builtins.__import__

        def controlled_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tomllib":
                raise ModuleNotFoundError("forced Python 3.10 TOML path")
            if name == "tomli":
                return fake_tomli
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=controlled_import):
            return work_plan._parse_top_level_toml_strings(path)

    def test_role_profile_requires_explicit_model_and_reasoning_effort(self) -> None:
        broken = Path(self.temp.name) / "broken-agents"
        broken.mkdir()
        (broken / "reviewer.toml").write_text(
            'name = "reviewer"\nsandbox_mode = "read-only"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(work_plan.PlanError, "must explicitly declare model"):
            work_plan.load_roles(broken)

    def test_role_profile_requires_reasoning_when_model_is_present(self) -> None:
        broken = Path(self.temp.name) / "missing-reasoning-agents"
        broken.mkdir()
        (broken / "reviewer.toml").write_text(
            'name = "reviewer"\nmodel = "test"\nsandbox_mode = "read-only"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            work_plan.PlanError,
            "must explicitly declare model_reasoning_effort",
        ):
            work_plan.load_roles(broken)

    def test_python310_tomli_fallback_ignores_nested_role_keys(self) -> None:
        role_file = Path(self.temp.name) / "nested-role.toml"
        role_file.write_text(
            'name = "reviewer"\n'
            'model = "top-level-model"\n'
            'model_reasoning_effort = "high"\n'
            'sandbox_mode = "read-only"\n'
            '\n'
            '[metadata]\n'
            'model = "nested-model"\n'
            'model_reasoning_effort = "low"\n',
            encoding="utf-8",
        )

        profile = self._parse_with_python310_tomli_fallback(role_file)

        self.assertEqual(profile["model"], "top-level-model")
        self.assertEqual(profile["model_reasoning_effort"], "high")

    def test_python310_tomli_fallback_accepts_trailing_comments(self) -> None:
        role_file = Path(self.temp.name) / "commented-role.toml"
        role_file.write_text(
            'name = "reviewer" # role identifier\n'
            'model = "comment-safe-model" # configured model\n'
            'model_reasoning_effort = "medium" # configured effort\n'
            'sandbox_mode = "read-only" # permission boundary\n',
            encoding="utf-8",
        )

        profile = self._parse_with_python310_tomli_fallback(role_file)

        self.assertEqual(
            profile,
            {
                "name": "reviewer",
                "model": "comment-safe-model",
                "model_reasoning_effort": "medium",
                "sandbox_mode": "read-only",
            },
        )

    def test_python310_requires_tomli_dependency(self) -> None:
        role_file = Path(self.temp.name) / "dependency-role.toml"
        role_file.write_text(
            'name = "reviewer"\n'
            'model = "test"\n'
            'model_reasoning_effort = "high"\n'
            'sandbox_mode = "read-only"\n',
            encoding="utf-8",
        )
        real_import = builtins.__import__

        def controlled_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in {"tomllib", "tomli"}:
                raise ModuleNotFoundError(f"forced missing dependency: {name}")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=controlled_import):
            with self.assertRaisesRegex(
                work_plan.PlanError,
                "Python 3.10 requires tomli>=2.0.1,<2.4",
            ):
                work_plan._parse_top_level_toml_strings(role_file)

    def test_valid_dependency_and_independent_review_create_three_waves(self) -> None:
        payload = draft(
            [
                task("scan-a1", "analyst", read_paths=["src"]),
                task(
                    "fix-a1",
                    "debugger",
                    read_paths=["src", "tests"],
                    write_paths=["src/fix.py", "tests/test_fix.py"],
                    depends_on=["scan-a1"],
                ),
                task(
                    "review-a1",
                    "reviewer",
                    read_paths=["src", "tests"],
                    independent_review=True,
                    review_of=["fix-a1"],
                ),
            ]
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(
            result["waves"],
            [
                {"wave": 1, "task_ids": ["scan-a1"]},
                {"wave": 2, "task_ids": ["fix-a1"]},
                {"wave": 3, "task_ids": ["review-a1"]},
            ],
        )
        self.assertEqual(result["ready_task_ids"], ["scan-a1"])

    def test_dependency_outcome_is_not_overridden_by_completed_runtime(self) -> None:
        for task_outcome in ("rejected", "role_mismatch", "failed", "blocked"):
            with self.subTest(task_outcome=task_outcome):
                runtime = [
                    {
                        "worker_id": "worker-upstream-a1",
                        "runtime_ref": "/root/upstream_a1",
                        "runtime_ref_source": "spawn_metadata",
                        "task_id": "upstream-a1",
                        "agent_type": "analyst",
                        "status": "completed",
                        "retired_from_followup": False,
                        "retirement_source": "unknown",
                        "read_paths": ["src/upstream"],
                        "write_paths": [],
                    }
                ]
                prior = [
                    {
                        "task_id": "upstream-a1",
                        "attempt": 1,
                        "status": task_outcome,
                        "worker_id": "worker-upstream-a1",
                    }
                ]
                payload = draft(
                    [
                        task(
                            "downstream-a1",
                            "analyst",
                            read_paths=["src/downstream"],
                            depends_on=["upstream-a1"],
                        )
                    ],
                    runtime_workers=runtime,
                    prior_tasks=prior,
                )

                result = work_plan.canonical_plan(payload, self.roles)

                self.assertEqual(result["ready_task_ids"], [])
                self.assertEqual(result["blocked_task_ids"], ["downstream-a1"])
                self.assertIn(
                    f"dependency upstream-a1 task outcome is {task_outcome}",
                    result["blocked_reasons"]["downstream-a1"][0],
                )

    def test_completed_runtime_without_task_outcome_does_not_resolve_dependency(self) -> None:
        runtime = [
            {
                "worker_id": "worker-upstream-a1",
                "runtime_ref": "/root/upstream_a1",
                "runtime_ref_source": "spawn_metadata",
                "task_id": "upstream-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src/upstream"],
                "write_paths": [],
            }
        ]
        payload = draft(
            [
                task(
                    "downstream-a1",
                    "analyst",
                    read_paths=["src/downstream"],
                    depends_on=["upstream-a1"],
                )
            ],
            runtime_workers=runtime,
        )

        result = work_plan.canonical_plan(payload, self.roles)

        self.assertEqual(result["ready_task_ids"], [])
        self.assertEqual(result["blocked_task_ids"], ["downstream-a1"])
        self.assertEqual(
            result["blocked_reasons"]["downstream-a1"],
            [
                "dependency upstream-a1 has no accepted task outcome "
                "(runtime status=completed)"
            ],
        )

    def test_write_conflict_is_split_into_separate_waves(self) -> None:
        payload = draft(
            [
                task("write-a1", "implementer", write_paths=["src/shared.py"]),
                task("write-b1", "debugger", read_paths=["src"], write_paths=["src/shared.py"]),
            ]
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(len(result["waves"]), 2)
        self.assertEqual(result["waves"][0]["task_ids"], ["write-a1"])
        self.assertEqual(result["waves"][1]["task_ids"], ["write-b1"])

    def test_read_only_role_cannot_declare_write_ownership(self) -> None:
        payload = draft([task("scan-a1", "analyst", write_paths=["src/file.py"])])
        with self.assertRaisesRegex(work_plan.PlanError, "read-only role"):
            work_plan.canonical_plan(payload, self.roles)

    def test_write_role_must_declare_write_ownership(self) -> None:
        payload = draft([task("impl-a1", "implementer", read_paths=["src"])])
        with self.assertRaisesRegex(work_plan.PlanError, "does not declare write ownership"):
            work_plan.canonical_plan(payload, self.roles)

    def test_completed_worker_does_not_consume_capacity(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "old-task-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src/old"],
                "write_paths": [],
            }
        ]
        payload = draft(
            [
                task("scan-a1", "analyst", read_paths=["src/a"]),
                task("scan-b1", "analyst", read_paths=["src/b"]),
            ],
            runtime_workers=runtime,
            max_workers=2,
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["open_workers"], 0)
        self.assertEqual(result["available_slots"], 2)
        self.assertEqual(result["ready_task_ids"], ["scan-a1", "scan-b1"])

    def test_running_worker_reduces_capacity_and_blocks_conflicting_task(self) -> None:
        runtime = [
            {
                "worker_id": "worker-live",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "live-task-a1",
                "agent_type": "implementer",
                "status": "running",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src/live"],
                "write_paths": ["src/live/state.py"],
            }
        ]
        payload = draft(
            [
                task("safe-a1", "analyst", read_paths=["src/other"]),
                task("conflict-a1", "analyst", read_paths=["src/live"]),
            ],
            runtime_workers=runtime,
            max_workers=2,
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["open_workers"], 1)
        self.assertEqual(result["available_slots"], 1)
        self.assertEqual(result["ready_task_ids"], ["safe-a1"])
        self.assertEqual(result["blocked_task_ids"], ["conflict-a1"])

    def test_retry_requires_new_id_second_attempt_and_stopped_old_worker(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "fix-old-a1",
                "agent_type": "debugger",
                "status": "stopped",
                "retired_from_followup": True,
                "retirement_source": "runtime_terminal_status",
                "read_paths": ["src"],
                "write_paths": ["src/fix.py"],
            }
        ]
        prior = [
            {
                "task_id": "fix-old-a1",
                "attempt": 1,
                "status": "failed",
                "worker_id": "worker-old",
            }
        ]
        payload = draft(
            [
                task(
                    "fix-new-a2",
                    "debugger",
                    write_paths=["src/fix.py"],
                    attempt=2,
                    replaces="fix-old-a1",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["ready_task_ids"], ["fix-new-a2"])

    def test_retry_rejects_running_old_worker_and_third_attempt(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "fix-old-a1",
                "agent_type": "debugger",
                "status": "running",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": ["src/fix.py"],
            }
        ]
        prior = [
            {
                "task_id": "fix-old-a1",
                "attempt": 1,
                "status": "failed",
                "worker_id": "worker-old",
            }
        ]
        payload = draft(
            [
                task(
                    "fix-new-a2",
                    "debugger",
                    write_paths=["src/fix.py"],
                    attempt=2,
                    replaces="fix-old-a1",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "inactive before replacement"):
            work_plan.canonical_plan(payload, self.roles)

        prior[0]["attempt"] = 2
        runtime[0]["status"] = "stopped"
        runtime[0]["retired_from_followup"] = True
        runtime[0]["retirement_source"] = "runtime_terminal_status"
        payload["tasks"][0]["attempt"] = 3
        with self.assertRaisesRegex(work_plan.PlanError, "two-attempt limit"):
            work_plan.canonical_plan(payload, self.roles)

    def test_reuse_requires_same_role_completed_state_and_delta_accounting(self) -> None:
        runtime = [
            {
                "worker_id": "worker-analysis",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "analysis-old-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft(
            [
                task(
                    "analysis-new-a1",
                    "analyst",
                    read_paths=["src"],
                    reuse_worker_id="worker-analysis",
                    accounting_scope="current_attempt_delta",
                )
            ],
            runtime_workers=runtime,
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["ready_task_ids"], ["analysis-new-a1"])

        payload["tasks"][0]["accounting_scope"] = "not_available"
        with self.assertRaisesRegex(work_plan.PlanError, "current_attempt_delta"):
            work_plan.canonical_plan(payload, self.roles)

    def test_independent_review_cannot_reuse_implementation_worker(self) -> None:
        runtime = [
            {
                "worker_id": "worker-impl",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "impl-old-a1",
                "agent_type": "reviewer",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        prior = [
            {
                "task_id": "impl-target-a1",
                "attempt": 1,
                "status": "accepted",
                "worker_id": None,
            }
        ]
        payload = draft(
            [
                task(
                    "review-new-a1",
                    "reviewer",
                    read_paths=["src"],
                    independent_review=True,
                    review_of=["impl-target-a1"],
                    reuse_worker_id="worker-impl",
                    accounting_scope="current_attempt_delta",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "fresh Worker"):
            work_plan.canonical_plan(payload, self.roles)

    def test_cycle_is_rejected(self) -> None:
        payload = draft(
            [
                task("scan-a1", "analyst", read_paths=["src/a"], depends_on=["scan-b1"]),
                task("scan-b1", "analyst", read_paths=["src/b"], depends_on=["scan-a1"]),
            ]
        )
        with self.assertRaisesRegex(work_plan.PlanError, "dependency cycle"):
            work_plan.canonical_plan(payload, self.roles)

    def test_generated_plan_validation_detects_manual_wave_edit(self) -> None:
        payload = draft(
            [
                task("scan-a1", "analyst", read_paths=["src/a"]),
                task("scan-b1", "analyst", read_paths=["src/b"]),
            ]
        )
        generated = work_plan.canonical_plan(payload, self.roles)
        validated = work_plan.validate_generated_plan(generated, self.roles)
        self.assertEqual(validated, generated)

        generated = json.loads(json.dumps(generated))
        generated["tasks"][0]["assigned_wave"] = 2
        with self.assertRaisesRegex(work_plan.PlanError, "tasks"):
            work_plan.validate_generated_plan(generated, self.roles)


    def test_runtime_ref_is_opaque_and_separate_from_worker_id(self) -> None:
        runtime = [
            {
                "worker_id": "worker-runtime-probe",
                "runtime_ref": "/root/runtime_metadata_probe",
                "runtime_ref_source": "thread_status_metadata",
                "task_id": "runtime-probe-a1",
                "agent_type": "default",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["."],
                "write_paths": [],
            }
        ]
        payload = draft([task("scan-a1", "analyst", read_paths=["src"])], runtime_workers=runtime)
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["runtime_workers"][0]["worker_id"], "worker-runtime-probe")
        self.assertEqual(result["runtime_workers"][0]["runtime_ref"], "/root/runtime_metadata_probe")

        runtime[0]["worker_id"] = "/root/runtime_metadata_probe"
        with self.assertRaisesRegex(work_plan.PlanError, "worker_id.*invalid identifier"):
            work_plan.canonical_plan(payload, self.roles)

    def test_execution_summary_renders_fixed_contract(self) -> None:
        payload = draft(
            [
                task("scan-a1", "default", read_paths=["."]),
                task("scan-b1", "default", read_paths=["pyproject.toml"]),
            ],
            max_workers=2,
        )
        plan = work_plan.canonical_plan(payload, self.roles)
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": "/root/scan_a1",
                    "runtime_ref_source": "spawn_metadata",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                },
                {
                    "task_id": "scan-b1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-b1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                },
            ],
            writes_observed=False,
        )
        summary = work_plan.build_execution_summary(plan, execution, self.roles)
        text = work_plan.render_execution_summary(summary)
        self.assertTrue(text.startswith("WORKPLAN_EXECUTION_SUMMARY\nsummary_version: 2\n"))
        self.assertIn('runtime_ref: "/root/scan_a1"', text)
        self.assertIn("runtime_ref_source: spawn_metadata", text)
        self.assertIn("runtime_ref: unknown", text)
        self.assertIn("runtime_ref_source: unknown", text)
        self.assertIn("task_outcome: accepted", text)
        self.assertIn('configured_model: "test"', text)
        self.assertIn('configured_model_reasoning_effort: "high"', text)
        self.assertIn("configured_sandbox_mode: read-only", text)
        self.assertIn("profile_source: agent_toml", text)
        self.assertIn('ready_task_ids: ["scan-a1", "scan-b1"]', text)
        self.assertIn("superseded_worker_ids: []", text)
        self.assertIn("active_workers_after_execution: 0", text)
        self.assertIn("writes_observed: false", text)

    def test_execution_record_requires_task_outcome(self) -> None:
        plan = work_plan.canonical_plan(
            draft([task("scan-a1", "default", read_paths=["."])]),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )
        execution["workers"][0].pop("task_outcome")
        with self.assertRaisesRegex(work_plan.PlanError, "task_outcome must be a non-empty string"):
            work_plan.build_execution_summary(plan, execution, self.roles)

    def test_active_worker_requires_not_evaluated_task_outcome(self) -> None:
        plan = work_plan.canonical_plan(
            draft([task("scan-a1", "default", read_paths=["."])]),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "running",
                    "task_outcome": "accepted",
                    "active_after_close": True,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
            active_worker_ids=["worker-scan-a1"],
        )
        with self.assertRaisesRegex(work_plan.PlanError, "must be not_evaluated"):
            work_plan.build_execution_summary(plan, execution, self.roles)

    def test_execution_digest_aggregates_profiles_and_independent_reviews(self) -> None:
        scan_plan = work_plan.canonical_plan(
            draft(
                [task("scan-a1", "default", read_paths=["."])],
                plan_id="scan-plan",
            ),
            self.roles,
        )
        scan_execution = execution_record(
            "scan-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
            writes_observed=False,
        )

        review_plan = work_plan.canonical_plan(
            draft(
                [
                    task(
                        "review-a1",
                        "critical_reviewer",
                        read_paths=["src"],
                        independent_review=True,
                        review_of=["impl-target-a1"],
                    )
                ],
                prior_tasks=[
                    {
                        "task_id": "impl-target-a1",
                        "attempt": 1,
                        "status": "accepted",
                        "worker_id": None,
                    }
                ],
                plan_id="review-plan",
            ),
            self.roles,
        )
        review_execution = execution_record(
            "review-plan",
            [
                {
                    "task_id": "review-a1",
                    "agent_type": "critical_reviewer",
                    "worker_id": "worker-review-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
            writes_observed=False,
        )

        digest = work_plan.build_execution_digest(
            [(scan_plan, scan_execution), (review_plan, review_execution)],
            self.roles,
        )
        self.assertEqual(digest["plan_count"], 2)
        self.assertEqual(digest["executed_attempt_count"], 2)
        self.assertEqual(digest["accepted_attempt_count"], 2)
        self.assertEqual(digest["independent_review_count"], 1)
        self.assertEqual(digest["writes_observed"]["observed_false"], 2)
        self.assertEqual(digest["anomalies"], [])

        profiles = {row["agent_type"]: row for row in digest["agent_profiles"]}
        self.assertEqual(profiles["critical_reviewer"]["configured_model"], "test")
        self.assertEqual(
            profiles["critical_reviewer"]["configured_model_reasoning_effort"],
            "high",
        )
        self.assertEqual(profiles["critical_reviewer"]["independent_review_count"], 1)

        text = work_plan.render_execution_digest(digest)
        self.assertIn("### 子任务执行概览", text)
        self.assertIn("`critical_reviewer`", text)
        self.assertIn("2/2 通过", text)
        self.assertIn("2/2 验收通过", text)
        self.assertIn("**异常：** 无。", text)
        self.assertNotIn("plan_command", text)
        self.assertNotIn("runtime_ref", text)
        self.assertNotIn("<details>", text)

    def test_execution_digest_does_not_treat_completed_role_mismatch_as_accepted(self) -> None:
        first_plan = work_plan.canonical_plan(
            draft(
                [task("pricing-explain-a1", "reviewer", read_paths=["pricing.py"])],
                plan_id="pricing-first-plan",
            ),
            self.roles,
        )
        first_execution = execution_record(
            "pricing-first-plan",
            [
                {
                    "task_id": "pricing-explain-a1",
                    "agent_type": "reviewer",
                    "worker_id": "worker-pricing-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "task_outcome": "role_mismatch",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )

        second_plan = work_plan.canonical_plan(
            draft(
                [
                    task(
                        "pricing-explain-a2",
                        "analyst",
                        read_paths=["pricing.py"],
                        attempt=2,
                        replaces="pricing-explain-a1",
                    )
                ],
                runtime_workers=[
                    {
                        "worker_id": "worker-pricing-a1",
                        "runtime_ref": None,
                        "runtime_ref_source": "unknown",
                        "task_id": "pricing-explain-a1",
                        "agent_type": "reviewer",
                        "status": "completed",
                        "retired_from_followup": False,
                        "retirement_source": "unknown",
                        "read_paths": ["pricing.py"],
                        "write_paths": [],
                    }
                ],
                prior_tasks=[
                    {
                        "task_id": "pricing-explain-a1",
                        "attempt": 1,
                        "status": "role_mismatch",
                        "worker_id": "worker-pricing-a1",
                    }
                ],
                plan_id="pricing-second-plan",
            ),
            self.roles,
        )
        second_execution = execution_record(
            "pricing-second-plan",
            [
                {
                    "task_id": "pricing-explain-a2",
                    "agent_type": "analyst",
                    "worker_id": "worker-pricing-a2",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "task_outcome": "accepted",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )

        digest = work_plan.build_execution_digest(
            [(first_plan, first_execution), (second_plan, second_execution)],
            self.roles,
        )
        self.assertEqual(digest["executed_attempt_count"], 2)
        self.assertEqual(digest["accepted_attempt_count"], 1)
        self.assertEqual(
            digest["task_outcome_counts"],
            {"accepted": 1, "role_mismatch": 1},
        )
        self.assertEqual(digest["runtime_status_counts"], {"completed": 2})
        self.assertEqual(digest["anomalies"][0]["type"], "non_accepted_attempts")
        self.assertEqual(
            digest["anomalies"][0]["attempts"][0]["task_outcome"],
            "role_mismatch",
        )

        profiles = {row["agent_type"]: row for row in digest["agent_profiles"]}
        self.assertEqual(profiles["reviewer"]["accepted_attempt_count"], 0)
        self.assertEqual(profiles["analyst"]["accepted_attempt_count"], 1)

        text = work_plan.render_execution_digest(digest)
        self.assertIn("1/2 验收通过", text)
        self.assertIn("pricing-explain-a1=role_mismatch", text)
        self.assertNotIn("2/2 成功", text)

    def test_execution_digest_uses_latest_active_snapshot_and_resolves_later_execution(self) -> None:
        first_plan = work_plan.canonical_plan(
            draft(
                [task("scan-a1", "default", read_paths=["."])],
                plan_id="first-plan",
            ),
            self.roles,
        )
        first_execution = execution_record(
            "first-plan",
            [],
            active_worker_ids=["worker-external"],
            writes_observed=False,
        )

        second_plan = work_plan.canonical_plan(
            draft(
                [task("scan-a1", "default", read_paths=["."])],
                plan_id="second-plan",
            ),
            self.roles,
        )
        second_execution = execution_record(
            "second-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
            active_worker_ids=[],
            writes_observed=False,
        )

        digest = work_plan.build_execution_digest(
            [(first_plan, first_execution), (second_plan, second_execution)],
            self.roles,
        )
        self.assertEqual(digest["not_executed_ready_task_ids"], [])
        self.assertEqual(digest["active_worker_ids_after_execution"], [])
        self.assertEqual(digest["anomalies"], [])

    def test_execution_digest_rejects_duplicate_plan(self) -> None:
        plan = work_plan.canonical_plan(
            draft([task("scan-a1", "default", read_paths=["."])]),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )
        with self.assertRaisesRegex(work_plan.PlanError, "duplicate plan_id"):
            work_plan.build_execution_digest(
                [(plan, execution), (plan, execution)],
                self.roles,
            )

    def test_execution_summary_rejects_task_not_ready(self) -> None:
        payload = draft(
            [
                task("scan-a1", "analyst", read_paths=["src"]),
                task("fix-a1", "debugger", read_paths=["src"], write_paths=["src/fix.py"], depends_on=["scan-a1"]),
            ]
        )
        plan = work_plan.canonical_plan(payload, self.roles)
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "fix-a1",
                    "agent_type": "debugger",
                    "worker_id": "worker-fix-a1",
                    "runtime_ref": "/root/fix_a1",
                    "runtime_ref_source": "spawn_metadata",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
            writes_observed=True,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "not in the validated ready_task_ids"):
            work_plan.build_execution_summary(plan, execution, self.roles)

    def test_independent_review_rejects_existing_runtime_ref(self) -> None:
        runtime = [
            {
                "worker_id": "worker-impl-a1",
                "runtime_ref": "/root/implementation_worker",
                "runtime_ref_source": "spawn_metadata",
                "task_id": "impl-target-a1",
                "agent_type": "implementer",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": ["src/implementation.py"],
            }
        ]
        prior = [
            {
                "task_id": "impl-target-a1",
                "attempt": 1,
                "status": "accepted",
                "worker_id": "worker-impl-a1",
            }
        ]
        plan = work_plan.canonical_plan(
            draft(
                [
                    task(
                        "review-new-a1",
                        "critical_reviewer",
                        read_paths=["src"],
                        independent_review=True,
                        review_of=["impl-target-a1"],
                    )
                ],
                runtime_workers=runtime,
                prior_tasks=prior,
            ),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "review-new-a1",
                    "agent_type": "critical_reviewer",
                    "worker_id": "worker-review-new-a1",
                    "runtime_ref": "/root/implementation_worker",
                    "runtime_ref_source": "thread_status_metadata",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )

        with self.assertRaisesRegex(work_plan.PlanError, "fresh runtime_ref"):
            work_plan.build_execution_digest([(plan, execution)], self.roles)

    def test_replacement_rejects_replaced_worker_runtime_ref(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old-a1",
                "runtime_ref": "/root/old_attempt_worker",
                "runtime_ref_source": "spawn_metadata",
                "task_id": "old-task-a1",
                "agent_type": "reviewer",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        prior = [
            {
                "task_id": "old-task-a1",
                "attempt": 1,
                "status": "role_mismatch",
                "worker_id": "worker-old-a1",
            }
        ]
        plan = work_plan.canonical_plan(
            draft(
                [
                    task(
                        "new-task-a2",
                        "analyst",
                        read_paths=["src"],
                        attempt=2,
                        replaces="old-task-a1",
                    )
                ],
                runtime_workers=runtime,
                prior_tasks=prior,
            ),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "new-task-a2",
                    "agent_type": "analyst",
                    "worker_id": "worker-new-task-a2",
                    "runtime_ref": "/root/old_attempt_worker",
                    "runtime_ref_source": "thread_status_metadata",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )

        with self.assertRaisesRegex(work_plan.PlanError, "fresh runtime_ref"):
            work_plan.build_execution_summary(plan, execution, self.roles)

    def test_execution_summary_validates_reused_runtime_identity(self) -> None:
        runtime = [
            {
                "worker_id": "worker-analysis",
                "runtime_ref": "/root/analysis_worker",
                "runtime_ref_source": "spawn_metadata",
                "task_id": "analysis-old-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft(
            [
                task(
                    "analysis-new-a1",
                    "analyst",
                    read_paths=["src"],
                    reuse_worker_id="worker-analysis",
                    accounting_scope="current_attempt_delta",
                )
            ],
            runtime_workers=runtime,
        )
        plan = work_plan.canonical_plan(payload, self.roles)
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "analysis-new-a1",
                    "agent_type": "analyst",
                    "worker_id": "worker-analysis",
                    "runtime_ref": "/root/wrong_worker",
                    "runtime_ref_source": "thread_status_metadata",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )
        with self.assertRaisesRegex(work_plan.PlanError, "runtime_ref does not match"):
            work_plan.build_execution_summary(plan, execution, self.roles)

    def test_execution_summary_active_state_must_match_final_status(self) -> None:
        plan = work_plan.canonical_plan(
            draft([task("scan-a1", "default", read_paths=["."])]),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": "/root/scan_a1",
                    "runtime_ref_source": "spawn_metadata",
                    "final_status": "completed",
                    "active_after_close": True,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
            active_worker_ids=["worker-scan-a1"],
        )
        with self.assertRaisesRegex(work_plan.PlanError, "inconsistent with final_status"):
            work_plan.build_execution_summary(plan, execution, self.roles)


    def test_runtime_ref_provenance_pairs_are_enforced(self) -> None:
        runtime = [
            {
                "worker_id": "worker-runtime-probe",
                "runtime_ref": "/root/runtime_metadata_probe",
                "runtime_ref_source": "unknown",
                "task_id": "runtime-probe-a1",
                "agent_type": "default",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["."],
                "write_paths": [],
            }
        ]
        payload = draft([task("scan-a1", "analyst", read_paths=["src"])], runtime_workers=runtime)
        runtime[0].pop("runtime_ref_source")
        with self.assertRaisesRegex(work_plan.PlanError, "runtime_ref_source must be a non-empty string"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["runtime_ref_source"] = "unknown"
        with self.assertRaisesRegex(work_plan.PlanError, "requires direct spawn_metadata"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["runtime_ref_source"] = "task_name"
        with self.assertRaisesRegex(work_plan.PlanError, "not runtime identity sources"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["runtime_ref"] = None
        runtime[0]["runtime_ref_source"] = "spawn_metadata"
        with self.assertRaisesRegex(work_plan.PlanError, "must be unknown when runtime_ref is null"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["runtime_ref_source"] = "unknown"
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertIsNone(result["runtime_workers"][0]["runtime_ref"])
        self.assertEqual(result["runtime_workers"][0]["runtime_ref_source"], "unknown")

    def test_execution_runtime_ref_provenance_gate(self) -> None:
        plan = work_plan.canonical_plan(
            draft([task("scan-a1", "default", read_paths=["."])]),
            self.roles,
        )
        worker = {
            "task_id": "scan-a1",
            "agent_type": "default",
            "worker_id": "worker-scan-a1",
            "runtime_ref": "/root/scan_a1",
            "runtime_ref_source": "unknown",
            "final_status": "completed",
            "active_after_close": False,
            "retired_from_followup": False,
            "retirement_source": "unknown",
        }
        execution = execution_record("test-plan", [worker])
        with self.assertRaisesRegex(work_plan.PlanError, "requires direct spawn_metadata"):
            work_plan.build_execution_summary(plan, execution, self.roles)

        worker["runtime_ref_source"] = "nickname"
        with self.assertRaisesRegex(work_plan.PlanError, "not runtime identity sources"):
            work_plan.build_execution_summary(plan, execution, self.roles)

        worker["runtime_ref"] = None
        worker["runtime_ref_source"] = "thread_status_metadata"
        with self.assertRaisesRegex(work_plan.PlanError, "must be unknown when runtime_ref is null"):
            work_plan.build_execution_summary(plan, execution, self.roles)

        worker["runtime_ref"] = "/root/scan_a1"
        worker["runtime_ref_source"] = "thread_status_metadata"
        summary = work_plan.build_execution_summary(plan, execution, self.roles)
        self.assertEqual(summary["workers"][0]["runtime_ref_source"], "thread_status_metadata")

    def test_reused_runtime_ref_may_be_reobserved_from_status_metadata(self) -> None:
        runtime = [
            {
                "worker_id": "worker-analysis",
                "runtime_ref": "/root/analysis_worker",
                "runtime_ref_source": "spawn_metadata",
                "task_id": "analysis-old-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        plan = work_plan.canonical_plan(
            draft(
                [
                    task(
                        "analysis-new-a1",
                        "analyst",
                        read_paths=["src"],
                        reuse_worker_id="worker-analysis",
                        accounting_scope="current_attempt_delta",
                    )
                ],
                runtime_workers=runtime,
            ),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "analysis-new-a1",
                    "agent_type": "analyst",
                    "worker_id": "worker-analysis",
                    "runtime_ref": "/root/analysis_worker",
                    "runtime_ref_source": "thread_status_metadata",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": False,
                    "retirement_source": "unknown",
                }
            ],
        )
        summary = work_plan.build_execution_summary(plan, execution, self.roles)
        self.assertEqual(summary["workers"][0]["runtime_ref"], "/root/analysis_worker")
        self.assertEqual(summary["workers"][0]["runtime_ref_source"], "thread_status_metadata")


    def test_role_mismatch_retry_accepts_completed_worker_after_reclaim_ack(self) -> None:
        runtime = [
            {
                "worker_id": "worker-pricing-a1",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "pricing-explain-a1",
                "agent_type": "reviewer",
                "status": "completed",
                "retired_from_followup": True,
                "retirement_source": "reclaim_response",
                "read_paths": ["pricing.py", "rules.py", "test_pricing.py"],
                "write_paths": [],
            }
        ]
        prior = [
            {
                "task_id": "pricing-explain-a1",
                "attempt": 1,
                "status": "role_mismatch",
                "worker_id": "worker-pricing-a1",
            }
        ]
        payload = draft(
            [
                task(
                    "pricing-explain-a2",
                    "analyst",
                    read_paths=["pricing.py", "rules.py", "test_pricing.py"],
                    attempt=2,
                    replaces="pricing-explain-a1",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["ready_task_ids"], ["pricing-explain-a2"])
        self.assertEqual(result["runtime_workers"][0]["status"], "completed")
        self.assertTrue(result["runtime_workers"][0]["retired_from_followup"])
        self.assertEqual(result["superseded_worker_ids"], ["worker-pricing-a1"])
        self.assertEqual(
            result["runtime_workers"][0]["superseded_by_task_id"],
            "pricing-explain-a2",
        )

    def test_retry_allows_inactive_completed_worker_without_retirement_evidence(self) -> None:
        runtime = [
            {
                "worker_id": "worker-pricing-a1",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "pricing-explain-a1",
                "agent_type": "reviewer",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["pricing.py"],
                "write_paths": [],
            }
        ]
        prior = [
            {
                "task_id": "pricing-explain-a1",
                "attempt": 1,
                "status": "role_mismatch",
                "worker_id": "worker-pricing-a1",
            }
        ]
        payload = draft(
            [
                task(
                    "pricing-explain-a2",
                    "analyst",
                    read_paths=["pricing.py"],
                    attempt=2,
                    replaces="pricing-explain-a1",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["ready_task_ids"], ["pricing-explain-a2"])
        self.assertEqual(result["superseded_worker_ids"], ["worker-pricing-a1"])
        self.assertEqual(
            result["runtime_workers"][0]["superseded_by_task_id"],
            "pricing-explain-a2",
        )
        self.assertFalse(result["runtime_workers"][0]["retired_from_followup"])
        self.assertEqual(result["runtime_workers"][0]["retirement_source"], "unknown")

    def test_reuse_rejects_retired_completed_worker(self) -> None:
        runtime = [
            {
                "worker_id": "worker-analysis",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "analysis-old-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": True,
                "retirement_source": "stop_response",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft(
            [
                task(
                    "analysis-new-a1",
                    "analyst",
                    read_paths=["src"],
                    reuse_worker_id="worker-analysis",
                    accounting_scope="current_attempt_delta",
                )
            ],
            runtime_workers=runtime,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "retired from follow-up"):
            work_plan.canonical_plan(payload, self.roles)

    def test_reuse_rejects_superseded_completed_worker(self) -> None:
        runtime = [
            {
                "worker_id": "worker-analysis",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "analysis-old-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "superseded_by_task_id": "analysis-retry-a2",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft(
            [
                task(
                    "analysis-new-a1",
                    "analyst",
                    read_paths=["src"],
                    reuse_worker_id="worker-analysis",
                    accounting_scope="current_attempt_delta",
                )
            ],
            runtime_workers=runtime,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "superseded by analysis-retry-a2"):
            work_plan.canonical_plan(payload, self.roles)

    def test_replacement_attempt_must_use_fresh_worker(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "old-task-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            },
            {
                "worker_id": "worker-other",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "other-task-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            },
        ]
        prior = [
            {
                "task_id": "old-task-a1",
                "attempt": 1,
                "status": "role_mismatch",
                "worker_id": "worker-old",
            }
        ]
        payload = draft(
            [
                task(
                    "new-task-a2",
                    "analyst",
                    read_paths=["src"],
                    attempt=2,
                    replaces="old-task-a1",
                    reuse_worker_id="worker-other",
                    accounting_scope="current_attempt_delta",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "replacement attempt must use a fresh Worker"):
            work_plan.canonical_plan(payload, self.roles)

    def test_active_worker_cannot_be_predeclared_superseded(self) -> None:
        runtime = [
            {
                "worker_id": "worker-live",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "live-task-a1",
                "agent_type": "analyst",
                "status": "running",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "superseded_by_task_id": "future-task-a2",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft([task("scan-a1", "analyst", read_paths=["src/next"])], runtime_workers=runtime)
        with self.assertRaisesRegex(work_plan.PlanError, "cannot be superseded"):
            work_plan.canonical_plan(payload, self.roles)

    def test_unknown_superseded_task_id_is_rejected(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "old-task-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "superseded_by_task_id": "missing-task-a2",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft([task("scan-a1", "analyst", read_paths=["src/next"])], runtime_workers=runtime)
        with self.assertRaisesRegex(work_plan.PlanError, "unknown superseded_by_task_id"):
            work_plan.canonical_plan(payload, self.roles)

    def test_second_replacement_of_same_worker_is_rejected(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "old-task-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": False,
                "retirement_source": "unknown",
                "superseded_by_task_id": "first-replacement-a2",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        prior = [
            {
                "task_id": "old-task-a1",
                "attempt": 1,
                "status": "role_mismatch",
                "worker_id": "worker-old",
            }
        ]
        payload = draft(
            [
                task(
                    "second-replacement-a2",
                    "analyst",
                    read_paths=["src"],
                    attempt=2,
                    replaces="old-task-a1",
                )
            ],
            runtime_workers=runtime,
            prior_tasks=prior,
        )
        with self.assertRaisesRegex(work_plan.PlanError, "already superseded by first-replacement-a2"):
            work_plan.canonical_plan(payload, self.roles)

    def test_retirement_provenance_pairs_are_enforced(self) -> None:
        runtime = [
            {
                "worker_id": "worker-old",
                "runtime_ref": None,
                "runtime_ref_source": "unknown",
                "task_id": "old-task-a1",
                "agent_type": "analyst",
                "status": "completed",
                "retired_from_followup": True,
                "retirement_source": "unknown",
                "read_paths": ["src"],
                "write_paths": [],
            }
        ]
        payload = draft([task("scan-a1", "analyst", read_paths=["src/next"])], runtime_workers=runtime)
        with self.assertRaisesRegex(work_plan.PlanError, "requires stop_response"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["retired_from_followup"] = False
        runtime[0]["retirement_source"] = "stop_response"
        with self.assertRaisesRegex(work_plan.PlanError, "must be unknown"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["retired_from_followup"] = True
        runtime[0]["retirement_source"] = "runtime_terminal_status"
        with self.assertRaisesRegex(work_plan.PlanError, "invalid for status=completed"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["status"] = "running"
        runtime[0]["retirement_source"] = "stop_response"
        with self.assertRaisesRegex(work_plan.PlanError, "cannot be retired"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["status"] = "stopped"
        runtime[0]["retired_from_followup"] = False
        runtime[0]["retirement_source"] = "unknown"
        with self.assertRaisesRegex(work_plan.PlanError, "terminal and requires"):
            work_plan.canonical_plan(payload, self.roles)

        runtime[0]["status"] = "completed"
        runtime[0]["retired_from_followup"] = True
        runtime[0]["retirement_source"] = "stop_response"
        result = work_plan.canonical_plan(payload, self.roles)
        self.assertEqual(result["runtime_workers"][0]["retirement_source"], "stop_response")

    def test_execution_summary_renders_retirement_evidence(self) -> None:
        plan = work_plan.canonical_plan(
            draft([task("scan-a1", "default", read_paths=["."])]),
            self.roles,
        )
        execution = execution_record(
            "test-plan",
            [
                {
                    "task_id": "scan-a1",
                    "agent_type": "default",
                    "worker_id": "worker-scan-a1",
                    "runtime_ref": None,
                    "runtime_ref_source": "unknown",
                    "final_status": "completed",
                    "active_after_close": False,
                    "retired_from_followup": True,
                    "retirement_source": "reclaim_response",
                }
            ],
        )
        summary = work_plan.build_execution_summary(plan, execution, self.roles)
        text = work_plan.render_execution_summary(summary)
        self.assertIn("retired_from_followup: true", text)
        self.assertIn("retirement_source: reclaim_response", text)


if __name__ == "__main__":
    unittest.main()
