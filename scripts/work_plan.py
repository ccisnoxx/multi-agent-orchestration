#!/usr/bin/env python3
"""Plan, validate, audit, and aggregate machine-verifiable SubAgent WorkPlans.

The planner is intentionally local and deterministic. It never creates agents,
changes files, calls a model, or makes network requests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, TypedDict

PLANNER_VERSION = "1.4.2"
SCHEMA_VERSION = 4

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

OPEN_WORKER_STATES = {"pending", "running"}
REUSABLE_WORKER_STATES = {"completed", "accepted"}
RUNTIME_WORKER_STATES = {
    "pending",
    "running",
    "completed",
    "accepted",
    "failed",
    "blocked",
    "early_stopped",
    "stopped",
    "interrupted",
    "reclaimed",
}
PRIOR_TASK_STATES = {
    "accepted",
    "failed",
    "blocked",
    "role_mismatch",
    "early_stopped",
    "interrupted",
    "rejected",
}
RETRYABLE_PRIOR_STATES = PRIOR_TASK_STATES - {"accepted"}
TASK_OUTCOME_STATES = PRIOR_TASK_STATES | {"not_evaluated"}
REVIEW_ROLES = {"reviewer", "critical_reviewer"}
ACCOUNTING_SCOPES = {"not_available", "current_attempt_delta"}
RUNTIME_REF_SOURCES = {"unknown", "spawn_metadata", "thread_status_metadata"}
OBSERVED_RUNTIME_REF_SOURCES = RUNTIME_REF_SOURCES - {"unknown"}
RETIREMENT_SOURCES = {
    "unknown",
    "stop_response",
    "reclaim_response",
    "runtime_terminal_status",
}
OBSERVED_RETIREMENT_SOURCES = RETIREMENT_SOURCES - {"unknown"}
TERMINAL_RETIREMENT_STATES = {
    "failed",
    "blocked",
    "early_stopped",
    "stopped",
    "interrupted",
    "reclaimed",
}
VALIDATE_STATUSES = {"passed"}
EXECUTION_RECORD_VERSION = 5
SUMMARY_VERSION = 2
DIGEST_VERSION = 2

ROLE_PROFILE_FIELDS = {
    "name",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
}

DRAFT_ROOT_FIELDS = {
    "version",
    "plan_id",
    "max_concurrent_workers",
    "runtime_workers",
    "prior_tasks",
    "tasks",
}
GENERATED_ROOT_FIELDS = DRAFT_ROOT_FIELDS | {
    "planner_version",
    "open_workers",
    "available_slots",
    "effective_capacity",
    "waves",
    "ready_task_ids",
    "deferred_task_ids",
    "blocked_task_ids",
    "blocked_reasons",
    "superseded_worker_ids",
}
TASK_FIELDS = {
    "task_id",
    "task_name",
    "agent_type",
    "attempt",
    "replaces_task_id",
    "depends_on",
    "read_paths",
    "write_paths",
    "independent_review",
    "review_of_task_ids",
    "reuse_worker_id",
    "accounting_scope",
    "deliverable",
    "acceptance_criteria",
    "assigned_wave",
}
RUNTIME_WORKER_FIELDS = {
    "worker_id",
    "runtime_ref",
    "runtime_ref_source",
    "task_id",
    "agent_type",
    "status",
    "retired_from_followup",
    "retirement_source",
    "superseded_by_task_id",
    "read_paths",
    "write_paths",
}
PRIOR_TASK_FIELDS = {
    "task_id",
    "attempt",
    "status",
    "worker_id",
}
EXECUTION_ROOT_FIELDS = {
    "version",
    "plan_id",
    "plan_command",
    "validate_command",
    "validate_status",
    "workers",
    "active_worker_ids_after_execution",
    "writes_observed",
}
EXECUTION_WORKER_FIELDS = {
    "task_id",
    "agent_type",
    "worker_id",
    "runtime_ref",
    "runtime_ref_source",
    "final_status",
    "task_outcome",
    "active_after_close",
    "retired_from_followup",
    "retirement_source",
}


class RoleProfile(TypedDict):
    name: str
    model: str
    model_reasoning_effort: str
    sandbox_mode: str
    config_file: str


class DependencyState(TypedDict):
    task_outcome: str | None
    runtime_status: str | None


RoleRegistry = dict[str, RoleProfile]


class PlanError(ValueError):
    """Raised when a draft or generated WorkPlan violates the contract."""


# ---------------------------------------------------------------------------
# Role discovery
# ---------------------------------------------------------------------------

def _toml_parser() -> Any:
    """Return a spec-compliant TOML parser for the active Python runtime.

    Python 3.11+ provides ``tomllib``. Python 3.10 must install the conditional
    ``tomli`` dependency declared by this Skill. A regex fallback is
    intentionally forbidden because it cannot preserve TOML table scope,
    comments, escaping, or multiline-string semantics.
    """
    try:
        import tomllib  # type: ignore

        return tomllib
    except ImportError:
        try:
            import tomli  # type: ignore

            return tomli
        except ImportError as exc:
            raise PlanError(
                "TOML parser unavailable: Python 3.10 requires "
                "tomli>=2.0.1,<2.4; install this Skill's requirements.txt "
                "or use Python 3.11+"
            ) from exc


def _parse_top_level_toml_strings(path: Path) -> dict[str, str]:
    """Parse Agent TOML and return only the required top-level string keys."""
    parser = _toml_parser()
    try:
        with path.open("rb") as handle:
            data = parser.load(handle)
    except parser.TOMLDecodeError as exc:
        raise PlanError(f"invalid TOML in {path.name}: {exc}") from exc

    return {
        key: value
        for key, value in data.items()
        if key in ROLE_PROFILE_FIELDS and isinstance(value, str)
    }


def default_agents_dir() -> Path:
    explicit = os.environ.get("CODEX_AGENTS_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".codex" / "agents"


def load_roles(agents_dir: Path) -> RoleRegistry:
    agents_dir = agents_dir.expanduser().resolve()
    if not agents_dir.is_dir():
        raise PlanError(f"agents directory does not exist: {agents_dir}")

    roles: RoleRegistry = {}
    for path in sorted(agents_dir.glob("*.toml")):
        data = _parse_top_level_toml_strings(path)
        name = data.get("name")
        sandbox = data.get("sandbox_mode")
        if not name or not ROLE_RE.fullmatch(name):
            continue
        if sandbox not in {"read-only", "workspace-write"}:
            continue

        model = data.get("model", "").strip()
        reasoning_effort = data.get("model_reasoning_effort", "").strip()
        if not model:
            raise PlanError(f"role {name} must explicitly declare model in {path.name}")
        if not reasoning_effort:
            raise PlanError(
                f"role {name} must explicitly declare model_reasoning_effort in {path.name}"
            )
        if len(model) > 128:
            raise PlanError(f"role {name} model exceeds 128 characters")
        if len(reasoning_effort) > 64:
            raise PlanError(f"role {name} model_reasoning_effort exceeds 64 characters")

        profile: RoleProfile = {
            "name": name,
            "model": model,
            "model_reasoning_effort": reasoning_effort,
            "sandbox_mode": sandbox,
            "config_file": path.name,
        }
        if name in roles and roles[name] != profile:
            raise PlanError(f"conflicting role definitions for {name}")
        roles[name] = profile

    if not roles:
        raise PlanError(f"no usable role TOML files found in: {agents_dir}")
    return roles


# ---------------------------------------------------------------------------
# Basic validation helpers
# ---------------------------------------------------------------------------

def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"{path} must be an object")
    return value


def _reject_extra_fields(obj: dict[str, Any], allowed: set[str], path: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise PlanError(f"{path} contains unsupported fields: {', '.join(extra)}")


def _require_nonempty_string(value: Any, path: str, max_length: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{path} must be a non-empty string")
    text = value.strip()
    if len(text) > max_length:
        raise PlanError(f"{path} exceeds {max_length} characters")
    if any(ord(char) < 32 and char not in "\t" for char in text):
        raise PlanError(f"{path} contains control characters")
    return text


def _require_identifier(value: Any, path: str, pattern: re.Pattern[str]) -> str:
    text = _require_nonempty_string(value, path, 128)
    if not pattern.fullmatch(text):
        raise PlanError(f"{path} has an invalid identifier format")
    return text


def _require_positive_int(value: Any, path: str) -> int:
    if type(value) is not int or value < 1:
        raise PlanError(f"{path} must be a positive integer")
    return value


def _string_list(
    value: Any,
    path: str,
    *,
    allow_empty: bool = True,
    identifier_pattern: re.Pattern[str] | None = None,
    max_items: int = 128,
) -> list[str]:
    if not isinstance(value, list):
        raise PlanError(f"{path} must be a list")
    if len(value) > max_items:
        raise PlanError(f"{path} contains too many items")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _require_nonempty_string(item, f"{path}[{index}]", 1000)
        if identifier_pattern and not identifier_pattern.fullmatch(text):
            raise PlanError(f"{path}[{index}] has an invalid identifier format")
        result.append(text)
    if not allow_empty and not result:
        raise PlanError(f"{path} must not be empty")
    if len(result) != len(set(result)):
        raise PlanError(f"{path} contains duplicate values")
    return result


def _optional_opaque_string(value: Any, path: str, max_length: int = 1024) -> str | None:
    if value is None:
        return None
    return _require_nonempty_string(value, path, max_length)


def _runtime_ref_identity(
    runtime_ref_value: Any,
    source_value: Any,
    path: str,
) -> tuple[str | None, str]:
    """Validate the runtime identity provenance pair.

    A runtime reference is accepted only when the record explicitly attributes
    it to direct runtime metadata. Display labels, Worker prose and local
    configuration are intentionally not supported sources.
    """
    runtime_ref = _optional_opaque_string(runtime_ref_value, f"{path}.runtime_ref")
    source = _require_nonempty_string(source_value, f"{path}.runtime_ref_source", 64)
    if source not in RUNTIME_REF_SOURCES:
        allowed = ", ".join(sorted(RUNTIME_REF_SOURCES))
        raise PlanError(
            f"{path}.runtime_ref_source must be one of: {allowed}; "
            "task_name, nickname, Worker self-report, TOML and config are not runtime identity sources"
        )
    if runtime_ref is None:
        if source != "unknown":
            raise PlanError(
                f"{path}.runtime_ref_source must be unknown when runtime_ref is null"
            )
    else:
        if runtime_ref == "unknown":
            raise PlanError(f"{path}.runtime_ref must use null, not the string 'unknown'")
        if source not in OBSERVED_RUNTIME_REF_SOURCES:
            raise PlanError(
                f"{path}.runtime_ref requires direct spawn_metadata or thread_status_metadata provenance"
            )
    return runtime_ref, source


def _retirement_identity(
    retired_value: Any,
    source_value: Any,
    status: str,
    path: str,
) -> tuple[bool, str]:
    """Validate orchestration retirement separately from runtime status.

    Some Codex surfaces keep a successfully stopped/reclaimed Worker visible as
    ``completed``. ``status`` therefore remains the observed runtime state,
    while ``retired_from_followup`` records only direct runtime retirement
    evidence. Fresh replacement eligibility is modeled separately through
    ``superseded_by_task_id`` and does not require unavailable acknowledgements.
    """
    if type(retired_value) is not bool:
        raise PlanError(f"{path}.retired_from_followup must be boolean")
    source = _require_nonempty_string(source_value, f"{path}.retirement_source", 64)
    if source not in RETIREMENT_SOURCES:
        allowed = ", ".join(sorted(RETIREMENT_SOURCES))
        raise PlanError(
            f"{path}.retirement_source must be one of: {allowed}; "
            "task_name, nickname, Worker self-report and orchestration intent are not retirement evidence"
        )

    if status in OPEN_WORKER_STATES:
        if retired_value or source != "unknown":
            raise PlanError(
                f"{path} is {status} and cannot be retired from follow-up"
            )
        return False, "unknown"

    if retired_value:
        if source not in OBSERVED_RETIREMENT_SOURCES:
            raise PlanError(
                f"{path}.retired_from_followup=true requires stop_response, "
                "reclaim_response, or runtime_terminal_status evidence"
            )
        if source == "runtime_terminal_status" and status not in TERMINAL_RETIREMENT_STATES:
            raise PlanError(
                f"{path}.retirement_source=runtime_terminal_status is invalid for status={status}"
            )
    else:
        if source != "unknown":
            raise PlanError(
                f"{path}.retirement_source must be unknown when retired_from_followup=false"
            )
        if status in TERMINAL_RETIREMENT_STATES:
            raise PlanError(
                f"{path}.status={status} is terminal and requires retired_from_followup=true"
            )

    return retired_value, source


def _command_list(value: Any, path: str) -> list[str]:
    return _string_list(value, path, allow_empty=False, max_items=128)


def _writes_observed(value: Any, path: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise PlanError(f"{path} must be true, false, or null")


def _task_outcome(value: Any, final_status: str, path: str) -> str:
    """Validate main-agent acceptance separately from runtime Worker status."""
    outcome = _require_nonempty_string(value, f"{path}.task_outcome", 32)
    if outcome not in TASK_OUTCOME_STATES:
        allowed = ", ".join(sorted(TASK_OUTCOME_STATES))
        raise PlanError(f"{path}.task_outcome must be one of: {allowed}")
    if final_status in OPEN_WORKER_STATES and outcome != "not_evaluated":
        raise PlanError(
            f"{path}.task_outcome must be not_evaluated while final_status={final_status}"
        )
    return outcome


def normalize_path(value: Any, path: str, *, write: bool = False) -> str:
    text = _require_nonempty_string(value, path, 1024).replace("\\", "/")
    pure = PurePosixPath(text)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", text):
        raise PlanError(f"{path} must be repository-relative")
    if ".." in pure.parts:
        raise PlanError(f"{path} must not contain parent traversal")
    if any(char in text for char in "*?[]"):
        raise PlanError(f"{path} must use exact paths, not globs")
    normalized = str(pure).rstrip("/") or "."
    if write and normalized == ".":
        raise PlanError(f"{path} must declare a narrower write ownership than repository root")
    return normalized


def normalize_paths(value: Any, path: str, *, write: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise PlanError(f"{path} must be a list")
    result = [normalize_path(item, f"{path}[{index}]", write=write) for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise PlanError(f"{path} contains duplicate normalized paths")
    return result


def paths_overlap(left: str, right: str) -> bool:
    return (
        left == "."
        or right == "."
        or left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def ownership_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_reads = left.get("read_paths", [])
    left_writes = left.get("write_paths", [])
    right_reads = right.get("read_paths", [])
    right_writes = right.get("write_paths", [])
    return (
        any(paths_overlap(a, b) for a in left_writes for b in right_writes)
        or any(paths_overlap(a, b) for a in left_writes for b in right_reads)
        or any(paths_overlap(a, b) for a in right_writes for b in left_reads)
    )


# ---------------------------------------------------------------------------
# Contract parsing
# ---------------------------------------------------------------------------

def parse_runtime_workers(value: Any, roles: RoleRegistry) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(value, list):
        raise PlanError("root.runtime_workers must be a list")
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    task_ids: set[str] = set()
    runtime_refs: set[str] = set()
    for index, raw in enumerate(value):
        path = f"root.runtime_workers[{index}]"
        row = _require_object(raw, path)
        _reject_extra_fields(row, RUNTIME_WORKER_FIELDS, path)
        worker_id = _require_identifier(row.get("worker_id"), f"{path}.worker_id", WORKER_ID_RE)
        runtime_ref, runtime_ref_source = _runtime_ref_identity(
            row.get("runtime_ref"), row.get("runtime_ref_source"), path
        )
        task_id = _require_identifier(row.get("task_id"), f"{path}.task_id", TASK_ID_RE)
        role = _require_identifier(row.get("agent_type"), f"{path}.agent_type", ROLE_RE)
        status = _require_nonempty_string(row.get("status"), f"{path}.status", 32)
        if role not in roles:
            raise PlanError(f"{path}.agent_type references an unavailable role: {role}")
        if status not in RUNTIME_WORKER_STATES:
            raise PlanError(f"{path}.status is unsupported: {status}")
        retired_from_followup, retirement_source = _retirement_identity(
            row.get("retired_from_followup"),
            row.get("retirement_source"),
            status,
            path,
        )
        superseded_by_task_id = row.get("superseded_by_task_id")
        if superseded_by_task_id is not None:
            superseded_by_task_id = _require_identifier(
                superseded_by_task_id,
                f"{path}.superseded_by_task_id",
                TASK_ID_RE,
            )
            if status in OPEN_WORKER_STATES:
                raise PlanError(
                    f"{path} is {status} and cannot be superseded for a fresh replacement"
                )
        read_paths = normalize_paths(row.get("read_paths", []), f"{path}.read_paths")
        write_paths = normalize_paths(row.get("write_paths", []), f"{path}.write_paths", write=True)
        if roles[role]["sandbox_mode"] == "read-only" and write_paths:
            raise PlanError(f"{path} uses read-only role {role} but declares write ownership")
        if status in OPEN_WORKER_STATES and not (read_paths or write_paths):
            raise PlanError(f"{path} is active but does not declare read/write ownership")
        if status in OPEN_WORKER_STATES and roles[role]["sandbox_mode"] == "workspace-write" and not write_paths:
            raise PlanError(f"{path} is an active write Worker but does not declare write ownership")
        if worker_id in by_id:
            raise PlanError(f"duplicate runtime worker_id: {worker_id}")
        if task_id in task_ids:
            raise PlanError(f"duplicate runtime task_id: {task_id}")
        if runtime_ref is not None and runtime_ref in runtime_refs:
            raise PlanError(f"duplicate runtime_ref: {runtime_ref}")
        normalized = {
            "worker_id": worker_id,
            "runtime_ref": runtime_ref,
            "runtime_ref_source": runtime_ref_source,
            "task_id": task_id,
            "agent_type": role,
            "status": status,
            "retired_from_followup": retired_from_followup,
            "retirement_source": retirement_source,
            "superseded_by_task_id": superseded_by_task_id,
            "read_paths": read_paths,
            "write_paths": write_paths,
        }
        rows.append(normalized)
        by_id[worker_id] = normalized
        task_ids.add(task_id)
        if runtime_ref is not None:
            runtime_refs.add(runtime_ref)
    return rows, by_id


def parse_prior_tasks(value: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(value, list):
        raise PlanError("root.prior_tasks must be a list")
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        path = f"root.prior_tasks[{index}]"
        row = _require_object(raw, path)
        _reject_extra_fields(row, PRIOR_TASK_FIELDS, path)
        task_id = _require_identifier(row.get("task_id"), f"{path}.task_id", TASK_ID_RE)
        attempt = _require_positive_int(row.get("attempt"), f"{path}.attempt")
        if attempt > 2:
            raise PlanError(f"{path}.attempt exceeds the two-attempt limit")
        status = _require_nonempty_string(row.get("status"), f"{path}.status", 32)
        if status not in PRIOR_TASK_STATES:
            raise PlanError(f"{path}.status is unsupported: {status}")
        worker_id = row.get("worker_id")
        if worker_id is not None:
            worker_id = _require_identifier(worker_id, f"{path}.worker_id", WORKER_ID_RE)
        if task_id in by_id:
            raise PlanError(f"duplicate prior task_id: {task_id}")
        normalized = {
            "task_id": task_id,
            "attempt": attempt,
            "status": status,
            "worker_id": worker_id,
        }
        rows.append(normalized)
        by_id[task_id] = normalized
    return rows, by_id


def parse_tasks(
    value: Any,
    roles: RoleRegistry,
    runtime_workers_by_id: dict[str, dict[str, Any]],
    prior_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(value, list) or not value:
        raise PlanError("root.tasks must be a non-empty list")
    if len(value) > 64:
        raise PlanError("root.tasks may contain at most 64 tasks")

    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    runtime_task_ids = {row["task_id"] for row in runtime_workers_by_id.values()}

    for index, raw in enumerate(value):
        path = f"root.tasks[{index}]"
        row = _require_object(raw, path)
        _reject_extra_fields(row, TASK_FIELDS, path)
        task_id = _require_identifier(row.get("task_id"), f"{path}.task_id", TASK_ID_RE)
        if task_id in by_id or task_id in prior_by_id or task_id in runtime_task_ids:
            raise PlanError(f"{path}.task_id must be fresh and unique: {task_id}")
        task_name = _require_nonempty_string(row.get("task_name"), f"{path}.task_name", 120)
        role = _require_identifier(row.get("agent_type"), f"{path}.agent_type", ROLE_RE)
        if role not in roles:
            raise PlanError(f"{path}.agent_type references an unavailable role: {role}")
        attempt = _require_positive_int(row.get("attempt", 1), f"{path}.attempt")
        if attempt > 2:
            raise PlanError(f"{path}.attempt exceeds the two-attempt limit")

        replaces = row.get("replaces_task_id")
        if replaces is not None:
            replaces = _require_identifier(replaces, f"{path}.replaces_task_id", TASK_ID_RE)
            if replaces == task_id:
                raise PlanError(f"{path}.replaces_task_id must use a different task ID")
            prior = prior_by_id.get(replaces)
            if prior is None:
                raise PlanError(f"{path}.replaces_task_id does not reference a prior task")
            if prior["status"] not in RETRYABLE_PRIOR_STATES:
                raise PlanError(f"{path} cannot retry prior task {replaces} with status={prior['status']}")
            if attempt != prior["attempt"] + 1:
                raise PlanError(f"{path}.attempt must equal prior attempt + 1")
            if attempt > 2:
                raise PlanError(f"{path} would exceed the two-attempt limit")
            prior_worker_id = prior.get("worker_id")
            if not prior_worker_id:
                raise PlanError(f"{path} cannot verify the old attempt Worker state: missing prior worker_id")
            prior_worker = runtime_workers_by_id.get(prior_worker_id)
            if prior_worker is None:
                raise PlanError(f"{path} cannot verify the old attempt Worker state: worker not present")
            if prior_worker["status"] in OPEN_WORKER_STATES:
                raise PlanError(
                    f"{path} requires the old attempt Worker to be inactive before replacement; "
                    f"current status={prior_worker['status']}"
                )
            superseded_by = prior_worker.get("superseded_by_task_id")
            if superseded_by not in (None, task_id):
                raise PlanError(
                    f"{path} cannot replace prior Worker {prior_worker_id}; "
                    f"it is already superseded by {superseded_by}"
                )
            # A completed/inactive Worker does not need an unavailable runtime
            # stop/reclaim acknowledgement before a fresh replacement. The
            # WorkPlan itself establishes the orchestration boundary: the old
            # Worker is superseded and must receive no further follow-up.
            prior_worker["superseded_by_task_id"] = task_id
        elif attempt != 1:
            raise PlanError(f"{path}.attempt > 1 requires replaces_task_id")

        depends_on = _string_list(
            row.get("depends_on", []),
            f"{path}.depends_on",
            identifier_pattern=TASK_ID_RE,
        )
        read_paths = normalize_paths(row.get("read_paths", []), f"{path}.read_paths")
        write_paths = normalize_paths(row.get("write_paths", []), f"{path}.write_paths", write=True)
        sandbox = roles[role]["sandbox_mode"]
        if sandbox == "read-only" and write_paths:
            raise PlanError(f"{path} uses read-only role {role} but declares write ownership")
        if sandbox == "read-only" and not read_paths:
            raise PlanError(f"{path} uses read-only role {role} but does not declare read ownership")
        if sandbox == "workspace-write" and not write_paths:
            raise PlanError(f"{path} uses write role {role} but does not declare write ownership")

        independent_review = row.get("independent_review", False)
        if type(independent_review) is not bool:
            raise PlanError(f"{path}.independent_review must be boolean")
        review_of = _string_list(
            row.get("review_of_task_ids", []),
            f"{path}.review_of_task_ids",
            identifier_pattern=TASK_ID_RE,
        )
        reuse_worker_id = row.get("reuse_worker_id")
        if reuse_worker_id is not None:
            reuse_worker_id = _require_identifier(reuse_worker_id, f"{path}.reuse_worker_id", WORKER_ID_RE)
        accounting_scope = _require_nonempty_string(
            row.get("accounting_scope", "not_available"),
            f"{path}.accounting_scope",
            64,
        )
        if accounting_scope not in ACCOUNTING_SCOPES:
            raise PlanError(f"{path}.accounting_scope is unsupported: {accounting_scope}")

        if replaces is not None and reuse_worker_id is not None:
            raise PlanError(f"{path} replacement attempt must use a fresh Worker")

        if independent_review:
            if role not in REVIEW_ROLES:
                raise PlanError(f"{path} marks independent_review but role {role} is not a review role")
            if not review_of:
                raise PlanError(f"{path}.review_of_task_ids must identify the implementation being reviewed")
            if reuse_worker_id is not None:
                raise PlanError(f"{path} independent review must use a fresh Worker")
        elif review_of:
            raise PlanError(f"{path}.review_of_task_ids requires independent_review=true")

        if reuse_worker_id is not None:
            worker = runtime_workers_by_id.get(reuse_worker_id)
            if worker is None:
                raise PlanError(f"{path}.reuse_worker_id does not reference a runtime Worker")
            if worker["status"] not in REUSABLE_WORKER_STATES:
                raise PlanError(f"{path} can only reuse a completed/accepted Worker")
            if worker["retired_from_followup"]:
                raise PlanError(
                    f"{path} cannot reuse Worker {reuse_worker_id} after it was retired from follow-up"
                )
            if worker.get("superseded_by_task_id") is not None:
                raise PlanError(
                    f"{path} cannot reuse Worker {reuse_worker_id} after it was superseded by "
                    f"{worker['superseded_by_task_id']}"
                )
            if worker["agent_type"] != role:
                raise PlanError(f"{path} cannot reuse Worker {reuse_worker_id} with a different agent_type")
            if accounting_scope != "current_attempt_delta":
                raise PlanError(f"{path} Worker reuse must use accounting_scope=current_attempt_delta")
            if replaces is not None and prior_by_id[replaces].get("worker_id") == reuse_worker_id:
                raise PlanError(f"{path} retry must not reuse the stopped Worker from the failed attempt")

        deliverable = _require_nonempty_string(row.get("deliverable"), f"{path}.deliverable", 2000)
        acceptance = _string_list(
            row.get("acceptance_criteria"),
            f"{path}.acceptance_criteria",
            allow_empty=False,
            max_items=32,
        )

        assigned_wave = row.get("assigned_wave")
        if assigned_wave is not None:
            _require_positive_int(assigned_wave, f"{path}.assigned_wave")

        normalized = {
            "task_id": task_id,
            "task_name": task_name,
            "agent_type": role,
            "attempt": attempt,
            "replaces_task_id": replaces,
            "depends_on": depends_on,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "independent_review": independent_review,
            "review_of_task_ids": review_of,
            "reuse_worker_id": reuse_worker_id,
            "accounting_scope": accounting_scope,
            "deliverable": deliverable,
            "acceptance_criteria": acceptance,
        }
        rows.append(normalized)
        by_id[task_id] = normalized

    return rows, by_id


# ---------------------------------------------------------------------------
# Dependency and planning logic
# ---------------------------------------------------------------------------

def _dependency_state_by_id(
    runtime_workers: Iterable[dict[str, Any]],
    prior_tasks: Iterable[dict[str, Any]],
) -> dict[str, DependencyState]:
    """Keep task acceptance and Worker lifecycle as separate dependency facts.

    ``prior_tasks.status`` is the authoritative task outcome. Runtime Worker
    status is retained only for diagnostics; a completed Worker does not prove
    that its task was accepted.
    """
    states: dict[str, DependencyState] = {}
    for row in runtime_workers:
        states[row["task_id"]] = {
            "task_outcome": None,
            "runtime_status": row["status"],
        }
    for row in prior_tasks:
        state = states.setdefault(
            row["task_id"],
            {"task_outcome": None, "runtime_status": None},
        )
        state["task_outcome"] = row["status"]
    return states


def _resolved_dependency(state: DependencyState) -> bool:
    return state["task_outcome"] == "accepted"


def _unresolved_dependency_reason(
    task_id: str,
    state: DependencyState,
) -> str:
    task_outcome = state["task_outcome"]
    runtime_status = state["runtime_status"]
    if task_outcome is not None:
        suffix = f" (runtime status={runtime_status})" if runtime_status is not None else ""
        return f"dependency {task_id} task outcome is {task_outcome}{suffix}"
    if runtime_status is not None:
        return (
            f"dependency {task_id} has no accepted task outcome "
            f"(runtime status={runtime_status})"
        )
    return f"dependency {task_id} has no accepted task outcome"


def build_dependencies(
    tasks: list[dict[str, Any]],
    tasks_by_id: dict[str, dict[str, Any]],
    external_states: dict[str, DependencyState],
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    graph: dict[str, set[str]] = {}
    unresolved_external: dict[str, list[str]] = defaultdict(list)

    known_ids = set(tasks_by_id) | set(external_states)
    for task in tasks:
        task_id = task["task_id"]
        dependencies = set(task["depends_on"]) | set(task["review_of_task_ids"])
        if task_id in dependencies:
            raise PlanError(f"task {task_id} cannot depend on itself")
        unknown = sorted(dependencies - known_ids)
        if unknown:
            raise PlanError(f"task {task_id} references unknown dependencies: {', '.join(unknown)}")
        internal = {dep for dep in dependencies if dep in tasks_by_id}
        graph[task_id] = internal
        for dep in sorted(dependencies - internal):
            state = external_states[dep]
            if not _resolved_dependency(state):
                unresolved_external[task_id].append(
                    _unresolved_dependency_reason(dep, state)
                )
    return graph, unresolved_external


def detect_cycle(graph: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            try:
                start = stack.index(node)
                cycle = stack[start:] + [node]
            except ValueError:
                cycle = stack + [node]
            raise PlanError("dependency cycle: " + " -> ".join(cycle))
        if node in visited:
            return
        visiting.add(node)
        for dep in sorted(graph[node]):
            visit(dep, stack + [node])
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node, [])


def active_conflicts(
    tasks: list[dict[str, Any]],
    runtime_workers: list[dict[str, Any]],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    active = [worker for worker in runtime_workers if worker["status"] in OPEN_WORKER_STATES]
    for task in tasks:
        for worker in active:
            if ownership_conflict(task, worker):
                result[task["task_id"]].append(
                    f"ownership conflicts with active worker {worker['worker_id']} ({worker['task_id']})"
                )
    return result


def propagate_blocked(
    graph: dict[str, set[str]],
    initial: dict[str, list[str]],
) -> dict[str, list[str]]:
    blocked: dict[str, list[str]] = {key: list(value) for key, value in initial.items()}
    changed = True
    while changed:
        changed = False
        for task_id, deps in graph.items():
            blocked_deps = sorted(dep for dep in deps if dep in blocked)
            if blocked_deps and task_id not in blocked:
                blocked[task_id] = ["depends on blocked task(s): " + ", ".join(blocked_deps)]
                changed = True
    return blocked


def assign_waves(
    tasks: list[dict[str, Any]],
    graph: dict[str, set[str]],
    blocked: dict[str, list[str]],
    max_concurrent: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    schedulable = [task for task in tasks if task["task_id"] not in blocked]
    by_id = {task["task_id"]: task for task in schedulable}
    assigned: dict[str, int] = {}
    waves: list[list[dict[str, Any]]] = []
    remaining = [task["task_id"] for task in schedulable]

    while remaining:
        progress = False
        for task_id in list(remaining):
            task = by_id[task_id]
            internal_deps = [dep for dep in graph[task_id] if dep in by_id]
            if any(dep not in assigned for dep in internal_deps):
                continue
            earliest = 1 + max((assigned[dep] for dep in internal_deps), default=0)
            wave_index = earliest - 1
            while True:
                while len(waves) <= wave_index:
                    waves.append([])
                wave = waves[wave_index]
                if len(wave) < max_concurrent and not any(ownership_conflict(task, peer) for peer in wave):
                    wave.append(task)
                    assigned[task_id] = wave_index + 1
                    remaining.remove(task_id)
                    progress = True
                    break
                wave_index += 1
        if not progress:
            raise PlanError("unable to assign waves; inspect dependencies and ownership declarations")

    normalized_tasks: list[dict[str, Any]] = []
    for task in tasks:
        item = dict(task)
        if task["task_id"] in assigned:
            item["assigned_wave"] = assigned[task["task_id"]]
        normalized_tasks.append(item)

    wave_rows = [
        {"wave": index + 1, "task_ids": [task["task_id"] for task in wave]}
        for index, wave in enumerate(waves)
        if wave
    ]
    return normalized_tasks, wave_rows


def canonical_plan(payload: dict[str, Any], roles: RoleRegistry) -> dict[str, Any]:
    root = _require_object(payload, "root")
    _reject_extra_fields(root, GENERATED_ROOT_FIELDS, "root")

    if root.get("version") != SCHEMA_VERSION:
        raise PlanError(f"root.version must equal {SCHEMA_VERSION}")
    plan_id = _require_identifier(root.get("plan_id"), "root.plan_id", PLAN_ID_RE)
    max_concurrent = _require_positive_int(root.get("max_concurrent_workers"), "root.max_concurrent_workers")
    if max_concurrent > 32:
        raise PlanError("root.max_concurrent_workers exceeds the safety limit of 32")

    runtime_workers, runtime_by_id = parse_runtime_workers(root.get("runtime_workers", []), roles)
    prior_tasks, prior_by_id = parse_prior_tasks(root.get("prior_tasks", []))
    tasks, tasks_by_id = parse_tasks(root.get("tasks"), roles, runtime_by_id, prior_by_id)

    known_task_ids = (
        {worker["task_id"] for worker in runtime_workers}
        | set(prior_by_id)
        | set(tasks_by_id)
    )
    for worker in runtime_workers:
        superseded_by = worker.get("superseded_by_task_id")
        if superseded_by is not None and superseded_by not in known_task_ids:
            raise PlanError(
                f"runtime Worker {worker['worker_id']} references unknown "
                f"superseded_by_task_id: {superseded_by}"
            )

    external_states = _dependency_state_by_id(runtime_workers, prior_tasks)
    graph, unresolved_external = build_dependencies(tasks, tasks_by_id, external_states)
    detect_cycle(graph)

    initial_blocked: dict[str, list[str]] = defaultdict(list)
    for task_id, reasons in unresolved_external.items():
        initial_blocked[task_id].extend(reasons)
    for task_id, reasons in active_conflicts(tasks, runtime_workers).items():
        initial_blocked[task_id].extend(reasons)
    blocked = propagate_blocked(graph, initial_blocked)

    planned_tasks, waves = assign_waves(tasks, graph, blocked, max_concurrent)

    open_workers = sum(worker["status"] in OPEN_WORKER_STATES for worker in runtime_workers)
    available_slots = max(0, max_concurrent - open_workers)
    first_wave_ids = waves[0]["task_ids"] if waves else []
    ready = first_wave_ids[:available_slots]
    assigned_ids = [task_id for wave in waves for task_id in wave["task_ids"]]
    deferred = [task_id for task_id in assigned_ids if task_id not in ready]
    blocked_ids = [task["task_id"] for task in tasks if task["task_id"] in blocked]
    superseded_worker_ids = [
        worker["worker_id"]
        for worker in runtime_workers
        if worker.get("superseded_by_task_id") is not None
    ]

    return {
        "version": SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "max_concurrent_workers": max_concurrent,
        "effective_capacity": max_concurrent,
        "open_workers": open_workers,
        "available_slots": available_slots,
        "runtime_workers": runtime_workers,
        "prior_tasks": prior_tasks,
        "tasks": planned_tasks,
        "waves": waves,
        "ready_task_ids": ready,
        "deferred_task_ids": deferred,
        "blocked_task_ids": blocked_ids,
        "blocked_reasons": {task_id: blocked[task_id] for task_id in blocked_ids},
        "superseded_worker_ids": superseded_worker_ids,
    }


def validate_generated_plan(payload: dict[str, Any], roles: RoleRegistry) -> dict[str, Any]:
    expected = canonical_plan(payload, roles)
    required_generated = {
        "planner_version",
        "effective_capacity",
        "open_workers",
        "available_slots",
        "waves",
        "ready_task_ids",
        "deferred_task_ids",
        "blocked_task_ids",
        "blocked_reasons",
        "superseded_worker_ids",
    }
    missing = sorted(required_generated - set(payload))
    if missing:
        raise PlanError("generated plan is missing fields: " + ", ".join(missing))

    # Normalize through JSON to avoid tuple/list or ordering surprises.
    for key in required_generated | {"tasks", "runtime_workers", "prior_tasks"}:
        if payload.get(key) != expected.get(key):
            raise PlanError(f"generated plan field does not match deterministic planner output: {key}")
    if payload.get("planner_version") != PLANNER_VERSION:
        raise PlanError(
            f"planner_version mismatch: expected {PLANNER_VERSION}, got {payload.get('planner_version')!r}"
        )
    return expected


# ---------------------------------------------------------------------------
# Execution summary contract
# ---------------------------------------------------------------------------

def parse_execution_record(
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    root = _require_object(payload, "execution")
    _reject_extra_fields(root, EXECUTION_ROOT_FIELDS, "execution")

    if root.get("version") != EXECUTION_RECORD_VERSION:
        raise PlanError(f"execution.version must equal {EXECUTION_RECORD_VERSION}")
    plan_id = _require_identifier(root.get("plan_id"), "execution.plan_id", PLAN_ID_RE)
    if plan_id != plan["plan_id"]:
        raise PlanError("execution.plan_id does not match the generated WorkPlan")

    plan_command = _command_list(root.get("plan_command"), "execution.plan_command")
    validate_command = _command_list(root.get("validate_command"), "execution.validate_command")
    validate_status = _require_nonempty_string(
        root.get("validate_status"), "execution.validate_status", 32
    )
    if validate_status not in VALIDATE_STATUSES:
        raise PlanError(f"execution.validate_status is unsupported: {validate_status}")

    raw_workers = root.get("workers")
    if not isinstance(raw_workers, list):
        raise PlanError("execution.workers must be a list")
    if len(raw_workers) > 64:
        raise PlanError("execution.workers may contain at most 64 entries")

    plan_tasks = {task["task_id"]: task for task in plan["tasks"]}
    ready_ids = set(plan["ready_task_ids"])
    runtime_workers = {row["worker_id"]: row for row in plan["runtime_workers"]}
    runtime_workers_by_ref = {
        row["runtime_ref"]: row
        for row in plan["runtime_workers"]
        if row["runtime_ref"] is not None
    }
    workers: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    seen_worker_ids: set[str] = set()
    seen_runtime_refs: set[str] = set()

    for index, raw in enumerate(raw_workers):
        path = f"execution.workers[{index}]"
        row = _require_object(raw, path)
        _reject_extra_fields(row, EXECUTION_WORKER_FIELDS, path)
        task_id = _require_identifier(row.get("task_id"), f"{path}.task_id", TASK_ID_RE)
        if task_id in seen_task_ids:
            raise PlanError(f"duplicate execution task_id: {task_id}")
        if task_id not in ready_ids:
            raise PlanError(f"{path}.task_id was not in the validated ready_task_ids: {task_id}")
        task = plan_tasks[task_id]

        agent_type = _require_identifier(row.get("agent_type"), f"{path}.agent_type", ROLE_RE)
        if agent_type != task["agent_type"]:
            raise PlanError(f"{path}.agent_type does not match the WorkPlan task")

        worker_id = _require_identifier(row.get("worker_id"), f"{path}.worker_id", WORKER_ID_RE)
        runtime_ref, runtime_ref_source = _runtime_ref_identity(
            row.get("runtime_ref"), row.get("runtime_ref_source"), path
        )
        final_status = _require_nonempty_string(row.get("final_status"), f"{path}.final_status", 32)
        if final_status not in RUNTIME_WORKER_STATES:
            raise PlanError(f"{path}.final_status is unsupported: {final_status}")
        task_outcome = _task_outcome(row.get("task_outcome"), final_status, path)
        active_after_close = row.get("active_after_close")
        if type(active_after_close) is not bool:
            raise PlanError(f"{path}.active_after_close must be boolean")
        expected_active = final_status in OPEN_WORKER_STATES
        if active_after_close != expected_active:
            raise PlanError(
                f"{path}.active_after_close is inconsistent with final_status={final_status}"
            )
        retired_from_followup, retirement_source = _retirement_identity(
            row.get("retired_from_followup"),
            row.get("retirement_source"),
            final_status,
            path,
        )

        reuse_worker_id = task.get("reuse_worker_id")
        known_runtime_ref_owner = (
            runtime_workers_by_ref.get(runtime_ref) if runtime_ref is not None else None
        )
        if reuse_worker_id is not None:
            if worker_id != reuse_worker_id:
                raise PlanError(f"{path}.worker_id must match the reused WorkPlan worker_id")
            planned_runtime_ref = runtime_workers[reuse_worker_id].get("runtime_ref")
            if planned_runtime_ref is not None and runtime_ref != planned_runtime_ref:
                raise PlanError(f"{path}.runtime_ref does not match the reused runtime Worker")
            if (
                known_runtime_ref_owner is not None
                and known_runtime_ref_owner["worker_id"] != reuse_worker_id
            ):
                raise PlanError(
                    f"{path}.runtime_ref belongs to a different runtime Worker: "
                    f"{known_runtime_ref_owner['worker_id']}"
                )
        else:
            if worker_id in runtime_workers:
                raise PlanError(f"{path} fresh task must use a fresh internal worker_id")
            if known_runtime_ref_owner is not None:
                raise PlanError(
                    f"{path} fresh task must use a fresh runtime_ref; "
                    f"reference belongs to existing Worker "
                    f"{known_runtime_ref_owner['worker_id']}"
                )

        if worker_id in seen_worker_ids:
            raise PlanError(f"duplicate execution worker_id: {worker_id}")
        if runtime_ref is not None and runtime_ref in seen_runtime_refs:
            raise PlanError(f"duplicate execution runtime_ref: {runtime_ref}")

        normalized = {
            "task_id": task_id,
            "agent_type": agent_type,
            "worker_id": worker_id,
            "runtime_ref": runtime_ref,
            "runtime_ref_source": runtime_ref_source,
            "final_status": final_status,
            "task_outcome": task_outcome,
            "active_after_close": active_after_close,
            "retired_from_followup": retired_from_followup,
            "retirement_source": retirement_source,
        }
        workers.append(normalized)
        seen_task_ids.add(task_id)
        seen_worker_ids.add(worker_id)
        if runtime_ref is not None:
            seen_runtime_refs.add(runtime_ref)

    active_worker_ids = _string_list(
        root.get("active_worker_ids_after_execution", []),
        "execution.active_worker_ids_after_execution",
        identifier_pattern=WORKER_ID_RE,
    )
    active_set = set(active_worker_ids)
    for index, worker in enumerate(workers):
        listed_active = worker["worker_id"] in active_set
        if listed_active != worker["active_after_close"]:
            raise PlanError(
                f"execution.workers[{index}].active_after_close disagrees with "
                "active_worker_ids_after_execution"
            )

    if validate_status == "failed" and workers:
        raise PlanError("execution.workers must be empty when validation failed")

    writes_observed = _writes_observed(root.get("writes_observed"), "execution.writes_observed")
    return {
        "version": EXECUTION_RECORD_VERSION,
        "plan_id": plan_id,
        "plan_command": plan_command,
        "validate_command": validate_command,
        "validate_status": validate_status,
        "workers": workers,
        "active_worker_ids_after_execution": active_worker_ids,
        "writes_observed": writes_observed,
    }


def build_execution_summary(
    plan_payload: dict[str, Any],
    execution_payload: dict[str, Any],
    roles: RoleRegistry,
) -> dict[str, Any]:
    plan = validate_generated_plan(plan_payload, roles)
    execution = parse_execution_record(execution_payload, plan)
    ready_order = {task_id: index for index, task_id in enumerate(plan["ready_task_ids"])}
    tasks_by_id = {task["task_id"]: task for task in plan["tasks"]}
    raw_workers = sorted(execution["workers"], key=lambda row: ready_order[row["task_id"]])

    workers: list[dict[str, Any]] = []
    for row in raw_workers:
        task = tasks_by_id[row["task_id"]]
        profile = roles[row["agent_type"]]
        workers.append(
            {
                **row,
                "task_name": task["task_name"],
                "independent_review": task["independent_review"],
                "configured_model": profile["model"],
                "configured_model_reasoning_effort": profile["model_reasoning_effort"],
                "configured_sandbox_mode": profile["sandbox_mode"],
                "profile_source": "agent_toml",
                "profile_file": profile["config_file"],
            }
        )

    executed_task_ids = [row["task_id"] for row in workers]
    executed_set = set(executed_task_ids)
    not_executed = [task_id for task_id in plan["ready_task_ids"] if task_id not in executed_set]
    return {
        "summary_type": "WORKPLAN_EXECUTION_SUMMARY",
        "summary_version": SUMMARY_VERSION,
        "planner_version": plan["planner_version"],
        "plan_command": execution["plan_command"],
        "validate_command": execution["validate_command"],
        "plan_id": plan["plan_id"],
        "validate_status": execution["validate_status"],
        "effective_capacity": plan["effective_capacity"],
        "open_workers_at_plan_time": plan["open_workers"],
        "available_slots_at_plan_time": plan["available_slots"],
        "waves": plan["waves"],
        "ready_task_ids": plan["ready_task_ids"],
        "executed_task_ids": executed_task_ids,
        "not_executed_ready_task_ids": not_executed,
        "superseded_worker_ids": plan["superseded_worker_ids"],
        "workers": workers,
        "active_workers_after_execution": len(execution["active_worker_ids_after_execution"]),
        "active_worker_ids_after_execution": execution["active_worker_ids_after_execution"],
        "writes_observed": execution["writes_observed"],
    }


def build_execution_digest(
    execution_entries: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    roles: RoleRegistry,
) -> dict[str, Any]:
    entries = list(execution_entries)
    if not entries:
        raise PlanError("execution digest requires at least one plan/execution entry")

    plan_ids: set[str] = set()
    attempt_ids: set[str] = set()
    superseded_worker_ids: set[str] = set()
    active_worker_ids: set[str] = set()
    not_executed_candidates: set[str] = set()
    runtime_status_counts: dict[str, int] = defaultdict(int)
    task_outcome_counts: dict[str, int] = defaultdict(int)
    role_stats: dict[str, dict[str, Any]] = {}
    non_accepted_attempts: list[dict[str, str]] = []
    unevaluated_attempts: list[dict[str, str]] = []
    writes_counts = {"observed_true": 0, "observed_false": 0, "unknown": 0}
    independent_review_count = 0
    accepted_attempt_count = 0

    for plan_payload, execution_payload in entries:
        summary = build_execution_summary(plan_payload, execution_payload, roles)
        plan_id = summary["plan_id"]
        if plan_id in plan_ids:
            raise PlanError(f"duplicate plan_id in execution digest: {plan_id}")
        plan_ids.add(plan_id)

        superseded_worker_ids.update(summary["superseded_worker_ids"])
        # Entries are chronological. The execution contract says each snapshot
        # lists all currently active Workers, so the latest snapshot is the
        # final session state rather than a union of historical states.
        active_worker_ids = set(summary["active_worker_ids_after_execution"])
        not_executed_candidates.update(summary["not_executed_ready_task_ids"])

        writes = summary["writes_observed"]
        if writes is True:
            writes_counts["observed_true"] += 1
        elif writes is False:
            writes_counts["observed_false"] += 1
        else:
            writes_counts["unknown"] += 1

        for worker in summary["workers"]:
            task_id = worker["task_id"]
            if task_id in attempt_ids:
                raise PlanError(f"duplicate executed task_id in execution digest: {task_id}")
            attempt_ids.add(task_id)

            runtime_status = worker["final_status"]
            runtime_status_counts[runtime_status] += 1
            task_outcome = worker["task_outcome"]
            task_outcome_counts[task_outcome] += 1
            if task_outcome == "accepted":
                accepted_attempt_count += 1
            elif task_outcome == "not_evaluated":
                unevaluated_attempts.append(
                    {"task_id": task_id, "runtime_status": runtime_status}
                )
            else:
                non_accepted_attempts.append(
                    {
                        "task_id": task_id,
                        "task_outcome": task_outcome,
                        "runtime_status": runtime_status,
                    }
                )

            if worker["independent_review"]:
                independent_review_count += 1

            agent_type = worker["agent_type"]
            profile_signature = (
                worker["configured_model"],
                worker["configured_model_reasoning_effort"],
                worker["configured_sandbox_mode"],
                worker["profile_source"],
                worker["profile_file"],
            )
            row = role_stats.get(agent_type)
            if row is None:
                row = {
                    "agent_type": agent_type,
                    "configured_model": worker["configured_model"],
                    "configured_model_reasoning_effort": worker["configured_model_reasoning_effort"],
                    "configured_sandbox_mode": worker["configured_sandbox_mode"],
                    "profile_source": worker["profile_source"],
                    "profile_file": worker["profile_file"],
                    "executed_attempt_count": 0,
                    "accepted_attempt_count": 0,
                    "independent_review_count": 0,
                    "runtime_status_counts": defaultdict(int),
                    "task_outcome_counts": defaultdict(int),
                    "_profile_signature": profile_signature,
                }
                role_stats[agent_type] = row
            elif row["_profile_signature"] != profile_signature:
                raise PlanError(f"conflicting role profile observed for agent_type: {agent_type}")

            row["executed_attempt_count"] += 1
            row["runtime_status_counts"][runtime_status] += 1
            row["task_outcome_counts"][task_outcome] += 1
            if task_outcome == "accepted":
                row["accepted_attempt_count"] += 1
            if worker["independent_review"]:
                row["independent_review_count"] += 1

    agent_profiles: list[dict[str, Any]] = []
    for agent_type in sorted(role_stats):
        row = role_stats[agent_type]
        row.pop("_profile_signature")
        row["runtime_status_counts"] = dict(sorted(row["runtime_status_counts"].items()))
        row["task_outcome_counts"] = dict(sorted(row["task_outcome_counts"].items()))
        agent_profiles.append(row)

    not_executed_ready_task_ids = not_executed_candidates - attempt_ids

    anomalies: list[dict[str, Any]] = []
    if non_accepted_attempts:
        anomalies.append(
            {"type": "non_accepted_attempts", "attempts": non_accepted_attempts}
        )
    if unevaluated_attempts:
        anomalies.append(
            {"type": "unevaluated_attempts", "attempts": unevaluated_attempts}
        )
    if not_executed_ready_task_ids:
        anomalies.append(
            {
                "type": "not_executed_ready_tasks",
                "task_ids": sorted(not_executed_ready_task_ids),
            }
        )
    if active_worker_ids:
        anomalies.append(
            {
                "type": "active_workers_after_execution",
                "worker_ids": sorted(active_worker_ids),
            }
        )
    if writes_counts["unknown"]:
        anomalies.append(
            {
                "type": "unknown_write_evidence",
                "plan_count": writes_counts["unknown"],
            }
        )

    return {
        "digest_type": "SUBAGENT_EXECUTION_DIGEST",
        "digest_version": DIGEST_VERSION,
        "profile_evidence": {
            "source": "agent_toml",
            "runtime_verified": False,
        },
        "outcome_evidence": {
            "source": "execution_record.task_outcome",
            "runtime_status_is_not_acceptance": True,
        },
        "plan_count": len(plan_ids),
        "validated_plan_count": len(plan_ids),
        "plan_ids": sorted(plan_ids),
        "executed_attempt_count": len(attempt_ids),
        "accepted_attempt_count": accepted_attempt_count,
        "independent_review_count": independent_review_count,
        "runtime_status_counts": dict(sorted(runtime_status_counts.items())),
        "task_outcome_counts": dict(sorted(task_outcome_counts.items())),
        "agent_profiles": agent_profiles,
        "not_executed_ready_task_ids": sorted(not_executed_ready_task_ids),
        "superseded_worker_ids": sorted(superseded_worker_ids),
        "active_worker_ids_after_execution": sorted(active_worker_ids),
        "writes_observed": writes_counts,
        "anomalies": anomalies,
    }


def _json_inline(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def render_execution_summary(summary: dict[str, Any]) -> str:
    writes = summary["writes_observed"]
    writes_text = "unknown" if writes is None else str(writes).lower()
    lines = [
        "WORKPLAN_EXECUTION_SUMMARY",
        f"summary_version: {summary['summary_version']}",
        f"planner_version: {summary['planner_version']}",
        f"plan_command: {_json_inline(summary['plan_command'])}",
        f"validate_command: {_json_inline(summary['validate_command'])}",
        f"plan_id: {summary['plan_id']}",
        f"validate_status: {summary['validate_status']}",
        f"effective_capacity: {summary['effective_capacity']}",
        f"open_workers_at_plan_time: {summary['open_workers_at_plan_time']}",
        f"available_slots_at_plan_time: {summary['available_slots_at_plan_time']}",
        "waves:",
    ]
    if summary["waves"]:
        for wave in summary["waves"]:
            lines.append(f"  - wave: {wave['wave']}")
            lines.append(f"    task_ids: {_json_inline(wave['task_ids'])}")
    else:
        lines.append("  []")
    lines.extend(
        [
            f"ready_task_ids: {_json_inline(summary['ready_task_ids'])}",
            f"executed_task_ids: {_json_inline(summary['executed_task_ids'])}",
            f"not_executed_ready_task_ids: {_json_inline(summary['not_executed_ready_task_ids'])}",
            f"superseded_worker_ids: {_json_inline(summary['superseded_worker_ids'])}",
            "workers:",
        ]
    )
    if summary["workers"]:
        for worker in summary["workers"]:
            runtime_ref = "unknown" if worker["runtime_ref"] is None else _json_inline(worker["runtime_ref"])
            lines.extend(
                [
                    f"  - task_id: {worker['task_id']}",
                    f"    task_name: {_json_inline(worker['task_name'])}",
                    f"    agent_type: {worker['agent_type']}",
                    f"    independent_review: {str(worker['independent_review']).lower()}",
                    f"    configured_model: {_json_inline(worker['configured_model'])}",
                    "    configured_model_reasoning_effort: "
                    + _json_inline(worker["configured_model_reasoning_effort"]),
                    f"    configured_sandbox_mode: {worker['configured_sandbox_mode']}",
                    f"    profile_source: {worker['profile_source']}",
                    f"    profile_file: {_json_inline(worker['profile_file'])}",
                    f"    worker_id: {worker['worker_id']}",
                    f"    runtime_ref: {runtime_ref}",
                    f"    runtime_ref_source: {worker['runtime_ref_source']}",
                    f"    final_status: {worker['final_status']}",
                    f"    task_outcome: {worker['task_outcome']}",
                    f"    active_after_close: {str(worker['active_after_close']).lower()}",
                    f"    retired_from_followup: {str(worker['retired_from_followup']).lower()}",
                    f"    retirement_source: {worker['retirement_source']}",
                ]
            )
    else:
        lines.append("  []")
    lines.extend(
        [
            f"active_workers_after_execution: {summary['active_workers_after_execution']}",
            "active_worker_ids_after_execution: "
            + _json_inline(summary["active_worker_ids_after_execution"]),
            f"writes_observed: {writes_text}",
        ]
    )
    return "\n".join(lines) + "\n"


def _markdown_code(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("`", "\\`")
    return f"`{text}`"


def render_execution_digest(digest: dict[str, Any]) -> str:
    lines = [
        "### 子任务执行概览",
        "",
        "| `agent_type` | 模型（Agent TOML） | 推理档位 | 执行尝试 | 验收通过 | 独立复核 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for profile in digest["agent_profiles"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_code(profile["agent_type"]),
                    _markdown_code(profile["configured_model"]),
                    _markdown_code(profile["configured_model_reasoning_effort"]),
                    str(profile["executed_attempt_count"]),
                    str(profile["accepted_attempt_count"]),
                    str(profile["independent_review_count"]),
                ]
            )
            + " |"
        )

    writes = digest["writes_observed"]
    lines.extend(
        [
            "",
            (
                f"计划校验：{digest['validated_plan_count']}/{digest['plan_count']} 通过；"
                "执行尝试："
                f"{digest['accepted_attempt_count']}/{digest['executed_attempt_count']} 验收通过；"
                f"独立复核：{digest['independent_review_count']}；"
                f"未执行 ready task：{len(digest['not_executed_ready_task_ids'])}；"
                f"残留活跃 Worker：{len(digest['active_worker_ids_after_execution'])}；"
                "写入观测："
                f"{writes['observed_true']} 有写入、"
                f"{writes['observed_false']} 无写入、"
                f"{writes['unknown']} 未知。"
            ),
        ]
    )

    if digest["anomalies"]:
        lines.extend(["", "**异常：**"])
        for anomaly in digest["anomalies"]:
            anomaly_type = anomaly["type"]
            if anomaly_type == "non_accepted_attempts":
                attempts = ", ".join(
                    f"{item['task_id']}={item['task_outcome']} "
                    f"(runtime={item['runtime_status']})"
                    for item in anomaly["attempts"]
                )
                lines.append(f"- 未验收通过的执行尝试：{attempts}")
            elif anomaly_type == "unevaluated_attempts":
                attempts = ", ".join(
                    f"{item['task_id']} (runtime={item['runtime_status']})"
                    for item in anomaly["attempts"]
                )
                lines.append(f"- 尚未验收的执行尝试：{attempts}")
            elif anomaly_type == "not_executed_ready_tasks":
                lines.append("- 未执行 ready task：" + ", ".join(anomaly["task_ids"]))
            elif anomaly_type == "active_workers_after_execution":
                lines.append("- 执行结束后仍活跃：" + ", ".join(anomaly["worker_ids"]))
            elif anomaly_type == "unknown_write_evidence":
                lines.append(f"- {anomaly['plan_count']} 个执行批次的写入证据未知")
    else:
        lines.extend(["", "**异常：** 无。"])

    lines.extend(
        [
            "",
            "模型和推理档位来自对应 Agent TOML 的显式配置，属于配置证据，"
            "不表示运行时接口已单独回报并验证这些值。",
            "验收通过仅按执行记录中的 `task_outcome=accepted` 统计；"
            "`final_status` 只表示 Worker 运行时状态，二者不得互相推导。",
        ]
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read input file: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    return _require_object(value, "root")


def _write_json(value: dict[str, Any], path: Path | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    _write_text(text, path)


def _write_text(text: str, path: Path | None) -> None:
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=default_agents_dir(),
        help="Directory containing the active custom Agent TOML files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Validate a draft and assign deterministic waves.")
    plan_parser.add_argument("input", type=Path)
    plan_parser.add_argument("--output", type=Path)

    validate_parser = subparsers.add_parser("validate", help="Recompute and validate a generated WorkPlan.")
    validate_parser.add_argument("input", type=Path)
    validate_parser.add_argument("--output", type=Path)

    summary_parser = subparsers.add_parser(
        "summary",
        help="Validate execution evidence and render WORKPLAN_EXECUTION_SUMMARY.",
    )
    summary_parser.add_argument("plan", type=Path, help="Validated generated WorkPlan JSON.")
    summary_parser.add_argument("execution", type=Path, help="Execution record JSON.")
    summary_parser.add_argument("--output", type=Path)
    summary_parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of text.")

    digest_parser = subparsers.add_parser(
        "digest",
        help="Validate and aggregate multiple WorkPlan executions into a compact digest.",
    )
    digest_parser.add_argument(
        "--entry",
        action="append",
        nargs=2,
        type=Path,
        required=True,
        metavar=("PLAN", "EXECUTION"),
        help="Validated generated WorkPlan JSON and its execution record JSON; repeat as needed.",
    )
    digest_parser.add_argument("--output", type=Path)
    digest_parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of Markdown.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        roles = load_roles(args.agents_dir)
        if args.command == "plan":
            result = canonical_plan(_read_json(args.input), roles)
            _write_json(result, args.output)
        elif args.command == "validate":
            result = validate_generated_plan(_read_json(args.input), roles)
            _write_json(result, args.output)
        elif args.command == "summary":
            summary = build_execution_summary(
                _read_json(args.plan),
                _read_json(args.execution),
                roles,
            )
            if args.json:
                _write_json(summary, args.output)
            else:
                _write_text(render_execution_summary(summary), args.output)
        else:
            digest = build_execution_digest(
                [(_read_json(plan), _read_json(execution)) for plan, execution in args.entry],
                roles,
            )
            if args.json:
                _write_json(digest, args.output)
            else:
                _write_text(render_execution_digest(digest), args.output)
        return 0
    except (PlanError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
