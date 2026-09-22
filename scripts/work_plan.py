#!/usr/bin/env python3
"""Plan, validate, audit, and aggregate deterministic Codex SubAgent WorkPlans.

The planner is intentionally local and deterministic. It never creates agents,
changes a target repository, calls a model, or makes network requests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, TypedDict

PLANNER_VERSION = "1.5.0"
SCHEMA_VERSION = 5
EXECUTION_RECORD_VERSION = 6
SUMMARY_VERSION = 3
DIGEST_VERSION = 3
DOCTOR_VERSION = 1

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
DISPATCH_METHODS = {"spawn_agent", "followup_task"}
DELIVERY_MODES = {"complete_task"}
PROGRESS_POLICIES = {"blocker_or_final"}

ROLE_PROFILE_FIELDS = {
    "name",
    "description",
    "developer_instructions",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
}
ROLE_PROFILE_EVIDENCE_FIELDS = {
    "name",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
    "profile_source",
    "config_file",
    "config_sha256",
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
    "effective_capacity",
    "open_workers",
    "available_slots",
    "role_profiles",
    "codex_config_evidence",
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
    "dispatch_method",
    "fork_turns",
    "delivery_mode",
    "progress_policy",
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
PRIOR_TASK_FIELDS = {"task_id", "attempt", "status", "worker_id"}
EXECUTION_ROOT_FIELDS = {
    "version",
    "plan_id",
    "plan_command",
    "validate_command",
    "validate_status",
    "workers",
    "active_worker_ids_after_execution",
    "writes_observed",
    "communication",
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
    "observed_dispatch",
    "observed_write_paths",
    "parent_followup_count",
    "worker_intermediate_message_count",
}
OBSERVED_DISPATCH_FIELDS = {
    "method",
    "task_name",
    "fork_turns",
    "model_override",
    "reasoning_effort_override",
}
COMMUNICATION_FIELDS = {"wait_call_count", "wait_timeout_count", "status_poll_count"}


class RoleProfile(TypedDict):
    name: str
    description: str
    developer_instructions: str
    model: str
    model_reasoning_effort: str
    sandbox_mode: str
    config_file: str
    config_sha256: str


class DependencyState(TypedDict):
    task_outcome: str | None
    runtime_status: str | None


RoleRegistry = dict[str, RoleProfile]


class PlanError(ValueError):
    """Raised when a plan, execution record, or configuration violates the contract."""


def _toml_parser() -> Any:
    try:
        import tomllib  # type: ignore

        return tomllib
    except ImportError:
        try:
            import tomli  # type: ignore

            return tomli
        except ImportError as exc:  # pragma: no cover - exercised on real Python 3.10
            raise PlanError(
                "TOML parser unavailable: Python 3.10 requires "
                "tomli>=2.0.1,<2.4; install requirements.txt or use Python 3.11+"
            ) from exc


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_toml(path: Path) -> tuple[dict[str, Any], bytes]:
    parser = _toml_parser()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read TOML file {path}: {exc}") from exc
    try:
        data = parser.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, parser.TOMLDecodeError) as exc:
        raise PlanError(f"invalid TOML in {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"TOML root must be a table: {path.name}")
    return data, raw


def default_agents_dir() -> Path:
    explicit = os.environ.get("CODEX_AGENTS_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".codex" / "agents"


def default_codex_config() -> Path:
    explicit = os.environ.get("CODEX_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".codex" / "config.toml"


def load_roles(agents_dir: Path) -> RoleRegistry:
    agents_dir = agents_dir.expanduser().resolve()
    if not agents_dir.is_dir():
        raise PlanError(f"agents directory does not exist: {agents_dir}")

    paths = sorted(agents_dir.glob("*.toml"))
    if not paths:
        raise PlanError(f"no Agent TOML files found in: {agents_dir}")

    roles: RoleRegistry = {}
    errors: list[str] = []
    for path in paths:
        try:
            data, raw = _load_toml(path)
            top_level = {
                key: value
                for key, value in data.items()
                if key in ROLE_PROFILE_FIELDS and isinstance(value, str)
            }
            name = top_level.get("name", "").strip()
            if not name or not ROLE_RE.fullmatch(name):
                raise PlanError("top-level name is missing or invalid")
            description = top_level.get("description", "").strip()
            instructions = top_level.get("developer_instructions", "").strip()
            model = top_level.get("model", "").strip()
            reasoning = top_level.get("model_reasoning_effort", "").strip()
            sandbox = top_level.get("sandbox_mode", "").strip()
            if not description:
                raise PlanError("top-level description is required")
            if not instructions:
                raise PlanError("top-level developer_instructions is required")
            if not model:
                raise PlanError("top-level model is required by the fixed-profile policy")
            if not reasoning:
                raise PlanError(
                    "top-level model_reasoning_effort is required by the fixed-profile policy"
                )
            if sandbox not in {"read-only", "workspace-write"}:
                raise PlanError("sandbox_mode must be read-only or workspace-write")
            if len(description) > 2000:
                raise PlanError("description exceeds 2000 characters")
            if len(instructions) > 20000:
                raise PlanError("developer_instructions exceeds 20000 characters")
            if len(model) > 128 or len(reasoning) > 64:
                raise PlanError("model or model_reasoning_effort is too long")

            profile: RoleProfile = {
                "name": name,
                "description": description,
                "developer_instructions": instructions,
                "model": model,
                "model_reasoning_effort": reasoning,
                "sandbox_mode": sandbox,
                "config_file": path.name,
                "config_sha256": _sha256_bytes(raw),
            }
            if name in roles:
                raise PlanError(
                    f"duplicate role name {name!r}; already provided by {roles[name]['config_file']}"
                )
            roles[name] = profile
        except PlanError as exc:
            errors.append(f"{path.name}: {exc}")

    if errors:
        raise PlanError("invalid Agent TOML files:\n- " + "\n- ".join(errors))
    return roles


def load_codex_config(path: Path, *, required: bool = False) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        if required:
            raise PlanError(f"Codex config does not exist: {path}")
        return {
            "exists": False,
            "config_file": path.name,
            "config_sha256": None,
            "configured_max_concurrent_threads_per_session": None,
            "raw": {},
        }
    data, raw = _load_toml(path)
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise PlanError("config.toml [agents] must be a table")
    configured = agents.get("max_concurrent_threads_per_session")
    if configured is not None and (type(configured) is not int or configured < 1):
        raise PlanError(
            "config.toml agents.max_concurrent_threads_per_session must be a positive integer"
        )
    if isinstance(configured, int) and configured > 32:
        raise PlanError(
            "config.toml agents.max_concurrent_threads_per_session exceeds safety limit 32"
        )
    return {
        "exists": True,
        "config_file": path.name,
        "config_sha256": _sha256_bytes(raw),
        "configured_max_concurrent_threads_per_session": configured,
        "raw": data,
    }


def role_profile_evidence(profile: RoleProfile) -> dict[str, Any]:
    return {
        "name": profile["name"],
        "model": profile["model"],
        "model_reasoning_effort": profile["model_reasoning_effort"],
        "sandbox_mode": profile["sandbox_mode"],
        "profile_source": "agent_toml",
        "config_file": profile["config_file"],
        "config_sha256": profile["config_sha256"],
    }


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


def _require_nonnegative_int(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        raise PlanError(f"{path} must be a non-negative integer")
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
    runtime_ref = _optional_opaque_string(runtime_ref_value, f"{path}.runtime_ref")
    source = _require_nonempty_string(source_value, f"{path}.runtime_ref_source", 64)
    if source not in RUNTIME_REF_SOURCES:
        allowed = ", ".join(sorted(RUNTIME_REF_SOURCES))
        raise PlanError(
            f"{path}.runtime_ref_source must be one of: {allowed}; display labels, "
            "Worker prose, TOML and local config are not runtime identity sources"
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
                f"{path}.runtime_ref requires spawn_metadata or thread_status_metadata provenance"
            )
    return runtime_ref, source


def _retirement_identity(
    retired_value: Any,
    source_value: Any,
    status: str,
    path: str,
) -> tuple[bool, str]:
    if type(retired_value) is not bool:
        raise PlanError(f"{path}.retired_from_followup must be boolean")
    source = _require_nonempty_string(source_value, f"{path}.retirement_source", 64)
    if source not in RETIREMENT_SOURCES:
        allowed = ", ".join(sorted(RETIREMENT_SOURCES))
        raise PlanError(f"{path}.retirement_source must be one of: {allowed}")
    if status in OPEN_WORKER_STATES:
        if retired_value or source != "unknown":
            raise PlanError(f"{path} is {status} and cannot be retired from follow-up")
        return False, "unknown"
    if retired_value:
        if source not in OBSERVED_RETIREMENT_SOURCES:
            raise PlanError(
                f"{path}.retired_from_followup=true requires direct retirement evidence"
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


def _task_outcome(value: Any, final_status: str, path: str) -> str:
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
        raise PlanError(f"{path} must declare narrower write ownership than repository root")
    return normalized


def normalize_paths(value: Any, path: str, *, write: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise PlanError(f"{path} must be a list")
    result = [
        normalize_path(item, f"{path}[{index}]", write=write)
        for index, item in enumerate(value)
    ]
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


def path_is_within(path: str, owner: str) -> bool:
    return owner == "." or path == owner or path.startswith(owner + "/")


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


def parse_runtime_workers(
    value: Any, roles: RoleRegistry
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
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
            raise PlanError(f"{path}.agent_type references unavailable role: {role}")
        if status not in RUNTIME_WORKER_STATES:
            raise PlanError(f"{path}.status is unsupported: {status}")
        retired, retirement_source = _retirement_identity(
            row.get("retired_from_followup"), row.get("retirement_source"), status, path
        )
        superseded = row.get("superseded_by_task_id")
        if superseded is not None:
            superseded = _require_identifier(
                superseded, f"{path}.superseded_by_task_id", TASK_ID_RE
            )
            if status in OPEN_WORKER_STATES:
                raise PlanError(f"{path} is active and cannot be superseded")
        read_paths = normalize_paths(row.get("read_paths", []), f"{path}.read_paths")
        write_paths = normalize_paths(
            row.get("write_paths", []), f"{path}.write_paths", write=True
        )
        sandbox = roles[role]["sandbox_mode"]
        if sandbox == "read-only" and write_paths:
            raise PlanError(f"{path} uses read-only role {role} but declares writes")
        if status in OPEN_WORKER_STATES and not (read_paths or write_paths):
            raise PlanError(f"{path} is active but declares no ownership")
        if status in OPEN_WORKER_STATES and sandbox == "workspace-write" and not write_paths:
            raise PlanError(f"{path} is an active write Worker without write ownership")
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
            "retired_from_followup": retired,
            "retirement_source": retirement_source,
            "superseded_by_task_id": superseded,
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
    task_names: set[str] = set()
    reused_worker_ids: set[str] = set()

    for index, raw in enumerate(value):
        path = f"root.tasks[{index}]"
        row = _require_object(raw, path)
        _reject_extra_fields(row, TASK_FIELDS, path)
        task_id = _require_identifier(row.get("task_id"), f"{path}.task_id", TASK_ID_RE)
        if task_id in by_id or task_id in prior_by_id or task_id in runtime_task_ids:
            raise PlanError(f"{path}.task_id must be fresh and unique: {task_id}")
        task_name = _require_nonempty_string(row.get("task_name"), f"{path}.task_name", 120)
        if task_name in task_names:
            raise PlanError(f"duplicate task_name in WorkPlan: {task_name}")
        role = _require_identifier(row.get("agent_type"), f"{path}.agent_type", ROLE_RE)
        if role not in roles:
            raise PlanError(f"{path}.agent_type references unavailable role: {role}")
        attempt = _require_positive_int(row.get("attempt", 1), f"{path}.attempt")
        if attempt > 2:
            raise PlanError(f"{path}.attempt exceeds the two-attempt limit")

        replaces = row.get("replaces_task_id")
        if replaces is not None:
            replaces = _require_identifier(replaces, f"{path}.replaces_task_id", TASK_ID_RE)
            if replaces == task_id:
                raise PlanError(f"{path}.replaces_task_id must differ from task_id")
            prior = prior_by_id.get(replaces)
            if prior is None:
                raise PlanError(f"{path}.replaces_task_id does not reference a prior task")
            if prior["status"] not in RETRYABLE_PRIOR_STATES:
                raise PlanError(
                    f"{path} cannot replace prior task {replaces} with status={prior['status']}"
                )
            if attempt != prior["attempt"] + 1 or attempt > 2:
                raise PlanError(f"{path}.attempt must equal prior attempt + 1 and stay <= 2")
            prior_worker_id = prior.get("worker_id")
            if not prior_worker_id:
                raise PlanError(f"{path} cannot verify old attempt: missing prior worker_id")
            prior_worker = runtime_workers_by_id.get(prior_worker_id)
            if prior_worker is None:
                raise PlanError(f"{path} cannot verify old attempt: Worker not present")
            if prior_worker["task_id"] != replaces:
                raise PlanError(
                    f"{path} prior task {replaces} references Worker {prior_worker_id}, "
                    f"but that Worker belongs to task {prior_worker['task_id']}"
                )
            if prior_worker["status"] in OPEN_WORKER_STATES:
                raise PlanError(
                    f"{path} requires old attempt Worker to be inactive; "
                    f"current status={prior_worker['status']}"
                )
            superseded_by = prior_worker.get("superseded_by_task_id")
            if superseded_by not in (None, task_id):
                raise PlanError(
                    f"{path} cannot replace Worker {prior_worker_id}; "
                    f"already superseded by {superseded_by}"
                )
            prior_worker["superseded_by_task_id"] = task_id
        elif attempt != 1:
            raise PlanError(f"{path}.attempt > 1 requires replaces_task_id")

        depends_on = _string_list(
            row.get("depends_on", []),
            f"{path}.depends_on",
            identifier_pattern=TASK_ID_RE,
        )
        read_paths = normalize_paths(row.get("read_paths", []), f"{path}.read_paths")
        write_paths = normalize_paths(
            row.get("write_paths", []), f"{path}.write_paths", write=True
        )
        sandbox = roles[role]["sandbox_mode"]
        if sandbox == "read-only" and write_paths:
            raise PlanError(f"{path} uses read-only role {role} but declares write ownership")
        if sandbox == "read-only" and not read_paths:
            raise PlanError(f"{path} uses read-only role {role} but declares no read ownership")
        if sandbox == "workspace-write" and not write_paths:
            raise PlanError(f"{path} uses write role {role} but declares no write ownership")

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
            reuse_worker_id = _require_identifier(
                reuse_worker_id, f"{path}.reuse_worker_id", WORKER_ID_RE
            )
        accounting_scope = _require_nonempty_string(
            row.get("accounting_scope"), f"{path}.accounting_scope", 64
        )
        if accounting_scope not in ACCOUNTING_SCOPES:
            raise PlanError(f"{path}.accounting_scope is unsupported: {accounting_scope}")

        dispatch_method = _require_nonempty_string(
            row.get("dispatch_method"), f"{path}.dispatch_method", 32
        )
        if dispatch_method not in DISPATCH_METHODS:
            raise PlanError(f"{path}.dispatch_method is unsupported: {dispatch_method}")
        fork_turns = row.get("fork_turns")
        delivery_mode = _require_nonempty_string(
            row.get("delivery_mode"), f"{path}.delivery_mode", 32
        )
        progress_policy = _require_nonempty_string(
            row.get("progress_policy"), f"{path}.progress_policy", 32
        )
        if delivery_mode not in DELIVERY_MODES:
            raise PlanError(f"{path}.delivery_mode must be complete_task")
        if progress_policy not in PROGRESS_POLICIES:
            raise PlanError(f"{path}.progress_policy must be blocker_or_final")

        if replaces is not None and reuse_worker_id is not None:
            raise PlanError(f"{path} replacement must use a fresh Worker")
        if independent_review:
            if role not in REVIEW_ROLES:
                raise PlanError(f"{path} independent review requires a review role")
            if not review_of:
                raise PlanError(f"{path}.review_of_task_ids must not be empty")
            if reuse_worker_id is not None:
                raise PlanError(f"{path} independent review must use a fresh Worker")
        elif review_of:
            raise PlanError(f"{path}.review_of_task_ids requires independent_review=true")

        if reuse_worker_id is None:
            if dispatch_method != "spawn_agent":
                raise PlanError(f"{path} fresh task must use dispatch_method=spawn_agent")
            if fork_turns != "none":
                raise PlanError(f"{path} fresh task must explicitly use fork_turns=none")
            if accounting_scope != "not_available":
                raise PlanError(f"{path} fresh task must use accounting_scope=not_available")
        else:
            if dispatch_method != "followup_task":
                raise PlanError(f"{path} reused Worker must use dispatch_method=followup_task")
            if fork_turns is not None:
                raise PlanError(f"{path} followup_task must use fork_turns=null")
            if reuse_worker_id in reused_worker_ids:
                raise PlanError(
                    f"runtime Worker {reuse_worker_id} may be reused by at most one task per WorkPlan"
                )
            worker = runtime_workers_by_id.get(reuse_worker_id)
            if worker is None:
                raise PlanError(f"{path}.reuse_worker_id does not reference a runtime Worker")
            if worker["status"] not in REUSABLE_WORKER_STATES:
                raise PlanError(f"{path} can only reuse a completed/accepted Worker")
            if worker["retired_from_followup"]:
                raise PlanError(f"{path} cannot reuse a retired Worker")
            if worker.get("superseded_by_task_id") is not None:
                raise PlanError(
                    f"{path} cannot reuse Worker superseded by {worker['superseded_by_task_id']}"
                )
            if worker["agent_type"] != role:
                raise PlanError(f"{path} cannot reuse Worker with a different agent_type")
            if accounting_scope != "current_attempt_delta":
                raise PlanError(
                    f"{path} reused Worker must use accounting_scope=current_attempt_delta"
                )
            reused_worker_ids.add(reuse_worker_id)

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
            "dispatch_method": dispatch_method,
            "fork_turns": fork_turns,
            "delivery_mode": delivery_mode,
            "progress_policy": progress_policy,
            "deliverable": deliverable,
            "acceptance_criteria": acceptance,
        }
        rows.append(normalized)
        by_id[task_id] = normalized
        task_names.add(task_name)
    return rows, by_id


def _dependency_state_by_id(
    runtime_workers: Iterable[dict[str, Any]], prior_tasks: Iterable[dict[str, Any]]
) -> dict[str, DependencyState]:
    states: dict[str, DependencyState] = {}
    for row in runtime_workers:
        states[row["task_id"]] = {
            "task_outcome": None,
            "runtime_status": row["status"],
        }
    for row in prior_tasks:
        state = states.setdefault(
            row["task_id"], {"task_outcome": None, "runtime_status": None}
        )
        state["task_outcome"] = row["status"]
    return states


def _unresolved_dependency_reason(task_id: str, state: DependencyState) -> str:
    outcome = state["task_outcome"]
    runtime = state["runtime_status"]
    if outcome is not None:
        suffix = f" (runtime status={runtime})" if runtime is not None else ""
        return f"dependency {task_id} task outcome is {outcome}{suffix}"
    if runtime is not None:
        return f"dependency {task_id} has no accepted task outcome (runtime status={runtime})"
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
            if state["task_outcome"] != "accepted":
                unresolved_external[task_id].append(
                    _unresolved_dependency_reason(dep, state)
                )
    return graph, unresolved_external


def detect_cycle(graph: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            start = stack.index(node) if node in stack else 0
            raise PlanError("dependency cycle: " + " -> ".join(stack[start:] + [node]))
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
    tasks: list[dict[str, Any]], runtime_workers: list[dict[str, Any]]
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
    graph: dict[str, set[str]], initial: dict[str, list[str]]
) -> dict[str, list[str]]:
    blocked = {key: list(value) for key, value in initial.items()}
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
    capacity: int,
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
            wave_index = max((assigned[dep] for dep in internal_deps), default=0)
            while True:
                while len(waves) <= wave_index:
                    waves.append([])
                wave = waves[wave_index]
                if len(wave) < capacity and not any(
                    ownership_conflict(task, peer) for peer in wave
                ):
                    wave.append(task)
                    assigned[task_id] = wave_index + 1
                    remaining.remove(task_id)
                    progress = True
                    break
                wave_index += 1
        if not progress:
            raise PlanError("unable to assign waves; inspect dependencies and ownership")

    planned_tasks: list[dict[str, Any]] = []
    for task in tasks:
        item = dict(task)
        if task["task_id"] in assigned:
            item["assigned_wave"] = assigned[task["task_id"]]
        planned_tasks.append(item)
    wave_rows = [
        {"wave": index + 1, "task_ids": [task["task_id"] for task in wave]}
        for index, wave in enumerate(waves)
        if wave
    ]
    return planned_tasks, wave_rows


def canonical_plan(
    payload: dict[str, Any], roles: RoleRegistry, codex_config: dict[str, Any]
) -> dict[str, Any]:
    root = _require_object(payload, "root")
    _reject_extra_fields(root, GENERATED_ROOT_FIELDS, "root")
    if root.get("version") != SCHEMA_VERSION:
        raise PlanError(f"root.version must equal {SCHEMA_VERSION}")
    plan_id = _require_identifier(root.get("plan_id"), "root.plan_id", PLAN_ID_RE)
    requested_capacity = _require_positive_int(
        root.get("max_concurrent_workers"), "root.max_concurrent_workers"
    )
    if requested_capacity > 32:
        raise PlanError("root.max_concurrent_workers exceeds safety limit 32")
    configured_capacity = codex_config.get(
        "configured_max_concurrent_threads_per_session"
    )
    effective_capacity = (
        min(requested_capacity, configured_capacity)
        if isinstance(configured_capacity, int)
        else requested_capacity
    )

    runtime_workers, runtime_by_id = parse_runtime_workers(
        root.get("runtime_workers", []), roles
    )
    prior_tasks, prior_by_id = parse_prior_tasks(root.get("prior_tasks", []))
    tasks, tasks_by_id = parse_tasks(root.get("tasks"), roles, runtime_by_id, prior_by_id)

    known_task_ids = {row["task_id"] for row in runtime_workers} | set(prior_by_id) | set(tasks_by_id)
    for worker in runtime_workers:
        superseded = worker.get("superseded_by_task_id")
        if superseded is not None and superseded not in known_task_ids:
            raise PlanError(
                f"runtime Worker {worker['worker_id']} references unknown "
                f"superseded_by_task_id: {superseded}"
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

    planned_tasks, waves = assign_waves(tasks, graph, blocked, effective_capacity)
    open_workers = sum(worker["status"] in OPEN_WORKER_STATES for worker in runtime_workers)
    available_slots = max(0, effective_capacity - open_workers)
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
    used_roles = sorted(
        {task["agent_type"] for task in tasks}
        | {worker["agent_type"] for worker in runtime_workers}
    )
    role_profiles = {
        role: role_profile_evidence(roles[role]) for role in used_roles
    }
    config_evidence = {
        "config_file": codex_config.get("config_file"),
        "config_sha256": codex_config.get("config_sha256"),
        "configured_max_concurrent_threads_per_session": configured_capacity,
        "capacity_source": "codex_config" if isinstance(configured_capacity, int) else "work_plan_only",
    }
    return {
        "version": SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "max_concurrent_workers": requested_capacity,
        "effective_capacity": effective_capacity,
        "open_workers": open_workers,
        "available_slots": available_slots,
        "role_profiles": role_profiles,
        "codex_config_evidence": config_evidence,
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


def validate_generated_plan(
    payload: dict[str, Any], roles: RoleRegistry, codex_config: dict[str, Any]
) -> dict[str, Any]:
    expected = canonical_plan(payload, roles, codex_config)
    required_generated = GENERATED_ROOT_FIELDS - DRAFT_ROOT_FIELDS
    missing = sorted(required_generated - set(payload))
    if missing:
        raise PlanError("generated plan is missing fields: " + ", ".join(missing))
    for key in required_generated | {"tasks", "runtime_workers", "prior_tasks"}:
        if payload.get(key) != expected.get(key):
            raise PlanError(
                f"generated plan field does not match deterministic output: {key}"
            )
    if payload.get("planner_version") != PLANNER_VERSION:
        raise PlanError(
            f"planner_version mismatch: expected {PLANNER_VERSION}, "
            f"got {payload.get('planner_version')!r}"
        )
    return expected


def _command_list(value: Any, path: str) -> list[str]:
    return _string_list(value, path, allow_empty=False, max_items=128)


def _parse_communication(value: Any) -> dict[str, int]:
    row = _require_object(value, "execution.communication")
    _reject_extra_fields(row, COMMUNICATION_FIELDS, "execution.communication")
    normalized = {
        key: _require_nonnegative_int(row.get(key), f"execution.communication.{key}")
        for key in sorted(COMMUNICATION_FIELDS)
    }
    if normalized["wait_timeout_count"] > normalized["wait_call_count"]:
        raise PlanError(
            "execution.communication.wait_timeout_count cannot exceed wait_call_count"
        )
    return normalized


def _parse_observed_dispatch(
    value: Any, task: dict[str, Any], path: str
) -> dict[str, Any]:
    row = _require_object(value, f"{path}.observed_dispatch")
    _reject_extra_fields(row, OBSERVED_DISPATCH_FIELDS, f"{path}.observed_dispatch")
    method = _require_nonempty_string(
        row.get("method"), f"{path}.observed_dispatch.method", 32
    )
    if method not in DISPATCH_METHODS:
        raise PlanError(f"{path}.observed_dispatch.method is unsupported: {method}")
    if method != task["dispatch_method"]:
        raise PlanError(f"{path}.observed_dispatch.method does not match WorkPlan")
    task_name = _require_nonempty_string(
        row.get("task_name"), f"{path}.observed_dispatch.task_name", 120
    )
    if task_name != task["task_name"]:
        raise PlanError(f"{path}.observed_dispatch.task_name does not match WorkPlan")
    fork_turns = row.get("fork_turns")
    if fork_turns != task["fork_turns"]:
        raise PlanError(f"{path}.observed_dispatch.fork_turns does not match WorkPlan")
    if method == "spawn_agent" and fork_turns != "none":
        raise PlanError(f"{path} spawn_agent must use fork_turns=none")
    if method == "followup_task" and fork_turns is not None:
        raise PlanError(f"{path} followup_task must use fork_turns=null")
    if row.get("model_override") is not None:
        raise PlanError(f"{path}.observed_dispatch.model_override must be null")
    if row.get("reasoning_effort_override") is not None:
        raise PlanError(
            f"{path}.observed_dispatch.reasoning_effort_override must be null"
        )
    return {
        "method": method,
        "task_name": task_name,
        "fork_turns": fork_turns,
        "model_override": None,
        "reasoning_effort_override": None,
    }


def _parse_observed_write_paths(
    value: Any, task: dict[str, Any], role: RoleProfile, path: str
) -> list[str] | None:
    if value is None:
        return None
    observed = normalize_paths(value, f"{path}.observed_write_paths", write=True)
    if role["sandbox_mode"] == "read-only" and observed:
        raise PlanError(f"{path} read-only role reported observed writes")
    for item in observed:
        if not any(path_is_within(item, owner) for owner in task["write_paths"]):
            raise PlanError(
                f"{path}.observed_write_paths contains path outside ownership: {item}"
            )
    return observed


def parse_execution_record(
    payload: dict[str, Any], plan: dict[str, Any], roles: RoleRegistry
) -> dict[str, Any]:
    root = _require_object(payload, "execution")
    _reject_extra_fields(root, EXECUTION_ROOT_FIELDS, "execution")
    if root.get("version") != EXECUTION_RECORD_VERSION:
        raise PlanError(f"execution.version must equal {EXECUTION_RECORD_VERSION}")
    plan_id = _require_identifier(root.get("plan_id"), "execution.plan_id", PLAN_ID_RE)
    if plan_id != plan["plan_id"]:
        raise PlanError("execution.plan_id does not match WorkPlan")
    plan_command = _command_list(root.get("plan_command"), "execution.plan_command")
    validate_command = _command_list(root.get("validate_command"), "execution.validate_command")
    validate_status = _require_nonempty_string(
        root.get("validate_status"), "execution.validate_status", 32
    )
    if validate_status not in VALIDATE_STATUSES:
        raise PlanError(f"execution.validate_status is unsupported: {validate_status}")
    communication = _parse_communication(root.get("communication"))

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
            raise PlanError(f"{path}.task_id was not in validated ready_task_ids: {task_id}")
        task = plan_tasks[task_id]
        agent_type = _require_identifier(row.get("agent_type"), f"{path}.agent_type", ROLE_RE)
        if agent_type != task["agent_type"]:
            raise PlanError(f"{path}.agent_type does not match WorkPlan")
        worker_id = _require_identifier(row.get("worker_id"), f"{path}.worker_id", WORKER_ID_RE)
        runtime_ref, runtime_ref_source = _runtime_ref_identity(
            row.get("runtime_ref"), row.get("runtime_ref_source"), path
        )
        final_status = _require_nonempty_string(
            row.get("final_status"), f"{path}.final_status", 32
        )
        if final_status not in RUNTIME_WORKER_STATES:
            raise PlanError(f"{path}.final_status is unsupported: {final_status}")
        task_outcome = _task_outcome(row.get("task_outcome"), final_status, path)
        active_after_close = row.get("active_after_close")
        if type(active_after_close) is not bool:
            raise PlanError(f"{path}.active_after_close must be boolean")
        if active_after_close != (final_status in OPEN_WORKER_STATES):
            raise PlanError(f"{path}.active_after_close is inconsistent with final_status")
        retired, retirement_source = _retirement_identity(
            row.get("retired_from_followup"),
            row.get("retirement_source"),
            final_status,
            path,
        )
        observed_dispatch = _parse_observed_dispatch(
            row.get("observed_dispatch"), task, path
        )
        observed_write_paths = _parse_observed_write_paths(
            row.get("observed_write_paths"), task, roles[agent_type], path
        )
        parent_followup_count = _require_nonnegative_int(
            row.get("parent_followup_count"), f"{path}.parent_followup_count"
        )
        intermediate_count = _require_nonnegative_int(
            row.get("worker_intermediate_message_count"),
            f"{path}.worker_intermediate_message_count",
        )

        reuse_worker_id = task.get("reuse_worker_id")
        known_ref_owner = runtime_workers_by_ref.get(runtime_ref) if runtime_ref is not None else None
        if reuse_worker_id is not None:
            if worker_id != reuse_worker_id:
                raise PlanError(f"{path}.worker_id must match reused Worker")
            planned_ref = runtime_workers[reuse_worker_id].get("runtime_ref")
            if planned_ref is not None and runtime_ref != planned_ref:
                raise PlanError(f"{path}.runtime_ref does not match reused Worker")
            if known_ref_owner is not None and known_ref_owner["worker_id"] != reuse_worker_id:
                raise PlanError(f"{path}.runtime_ref belongs to another Worker")
        else:
            if worker_id in runtime_workers:
                raise PlanError(f"{path} fresh task must use a fresh internal worker_id")
            if known_ref_owner is not None:
                raise PlanError(
                    f"{path} fresh task must use a fresh runtime_ref; existing owner is "
                    f"{known_ref_owner['worker_id']}"
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
            "retired_from_followup": retired,
            "retirement_source": retirement_source,
            "observed_dispatch": observed_dispatch,
            "observed_write_paths": observed_write_paths,
            "parent_followup_count": parent_followup_count,
            "worker_intermediate_message_count": intermediate_count,
        }
        workers.append(normalized)
        seen_task_ids.add(task_id)
        seen_worker_ids.add(worker_id)
        if runtime_ref is not None:
            seen_runtime_refs.add(runtime_ref)

    active_ids = _string_list(
        root.get("active_worker_ids_after_execution", []),
        "execution.active_worker_ids_after_execution",
        identifier_pattern=WORKER_ID_RE,
    )
    active_set = set(active_ids)
    for index, worker in enumerate(workers):
        if (worker["worker_id"] in active_set) != worker["active_after_close"]:
            raise PlanError(
                f"execution.workers[{index}].active_after_close disagrees with active Worker list"
            )

    raw_writes = root.get("writes_observed")
    if raw_writes is not None and type(raw_writes) is not bool:
        raise PlanError("execution.writes_observed must be true, false, or null")
    observed_values = [worker["observed_write_paths"] for worker in workers]
    if any(value for value in observed_values if isinstance(value, list)):
        expected_writes: bool | None = True
    elif any(value is None for value in observed_values):
        expected_writes = None
    else:
        expected_writes = False
    if raw_writes is not expected_writes:
        raise PlanError(
            "execution.writes_observed is inconsistent with observed_write_paths"
        )

    return {
        "version": EXECUTION_RECORD_VERSION,
        "plan_id": plan_id,
        "plan_command": plan_command,
        "validate_command": validate_command,
        "validate_status": validate_status,
        "workers": workers,
        "active_worker_ids_after_execution": active_ids,
        "writes_observed": raw_writes,
        "communication": communication,
    }


def guard_dispatch(
    plan_payload: dict[str, Any],
    task_id: str,
    dispatch_payload: dict[str, Any],
    roles: RoleRegistry,
    codex_config: dict[str, Any],
) -> dict[str, Any]:
    """Validate an actual dispatch payload before a runtime tool call.

    This command is intentionally transport-neutral so it can be called from a
    local hook adapter without depending on one Codex hook JSON envelope.
    """
    plan = validate_generated_plan(plan_payload, roles, codex_config)
    task_id = _require_identifier(task_id, "guard.task_id", TASK_ID_RE)
    if task_id not in plan["ready_task_ids"]:
        raise PlanError(f"guard.task_id is not currently ready: {task_id}")
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    task = tasks[task_id]
    row = _require_object(dispatch_payload, "guard.dispatch")
    allowed = {
        "method",
        "task_name",
        "agent_type",
        "fork_turns",
        "model",
        "reasoning_effort",
    }
    _reject_extra_fields(row, allowed, "guard.dispatch")
    method = _require_nonempty_string(row.get("method"), "guard.dispatch.method", 32)
    if method != task["dispatch_method"]:
        raise PlanError(
            f"guard.dispatch.method must equal planned {task['dispatch_method']}"
        )
    task_name = _require_nonempty_string(
        row.get("task_name"), "guard.dispatch.task_name", 120
    )
    if task_name != task["task_name"]:
        raise PlanError("guard.dispatch.task_name does not match WorkPlan")
    agent_type = _require_identifier(
        row.get("agent_type"), "guard.dispatch.agent_type", ROLE_RE
    )
    if agent_type != task["agent_type"]:
        raise PlanError("guard.dispatch.agent_type does not match WorkPlan")
    fork_turns = row.get("fork_turns")
    if fork_turns != task["fork_turns"]:
        raise PlanError("guard.dispatch.fork_turns does not match WorkPlan")
    if method == "spawn_agent" and fork_turns != "none":
        raise PlanError("guard dispatch rejects fresh spawn without fork_turns=none")
    if method == "followup_task" and fork_turns is not None:
        raise PlanError("guard dispatch requires fork_turns=null for followup_task")
    if row.get("model") is not None:
        raise PlanError("guard dispatch rejects model override under fixed-profile policy")
    if row.get("reasoning_effort") is not None:
        raise PlanError(
            "guard dispatch rejects reasoning_effort override under fixed-profile policy"
        )
    return {
        "guard_type": "DISPATCH_CONTRACT_GUARD",
        "planner_version": PLANNER_VERSION,
        "plan_id": plan["plan_id"],
        "task_id": task_id,
        "status": "passed",
        "observed_dispatch": {
            "method": method,
            "task_name": task_name,
            "fork_turns": fork_turns,
            "model_override": None,
            "reasoning_effort_override": None,
        },
    }


def build_execution_summary(
    plan_payload: dict[str, Any],
    execution_payload: dict[str, Any],
    roles: RoleRegistry,
    codex_config: dict[str, Any],
) -> dict[str, Any]:
    plan = validate_generated_plan(plan_payload, roles, codex_config)
    execution = parse_execution_record(execution_payload, plan, roles)
    ready_order = {task_id: index for index, task_id in enumerate(plan["ready_task_ids"])}
    tasks_by_id = {task["task_id"]: task for task in plan["tasks"]}
    raw_workers = sorted(execution["workers"], key=lambda row: ready_order[row["task_id"]])
    workers: list[dict[str, Any]] = []
    unknown_write_tasks: list[str] = []
    for row in raw_workers:
        task = tasks_by_id[row["task_id"]]
        profile = plan["role_profiles"][row["agent_type"]]
        if row["observed_write_paths"] is None:
            unknown_write_tasks.append(row["task_id"])
        workers.append(
            {
                **row,
                "task_name": task["task_name"],
                "independent_review": task["independent_review"],
                "planned_write_paths": task["write_paths"],
                "configured_model": profile["model"],
                "configured_model_reasoning_effort": profile["model_reasoning_effort"],
                "configured_sandbox_mode": profile["sandbox_mode"],
                "profile_source": profile["profile_source"],
                "profile_file": profile["config_file"],
                "profile_sha256": profile["config_sha256"],
            }
        )
    executed_task_ids = [row["task_id"] for row in workers]
    executed_set = set(executed_task_ids)
    not_executed = [task_id for task_id in plan["ready_task_ids"] if task_id not in executed_set]
    dispatch_counts = {
        "fresh_spawn_count": sum(
            row["observed_dispatch"]["method"] == "spawn_agent" for row in workers
        ),
        "reused_followup_count": sum(
            row["observed_dispatch"]["method"] == "followup_task" for row in workers
        ),
        "isolated_fork_count": sum(
            row["observed_dispatch"]["fork_turns"] == "none" for row in workers
        ),
    }
    return {
        "summary_type": "WORKPLAN_EXECUTION_SUMMARY",
        "summary_version": SUMMARY_VERSION,
        "planner_version": plan["planner_version"],
        "plan_id": plan["plan_id"],
        "plan_command": execution["plan_command"],
        "validate_command": execution["validate_command"],
        "validate_status": execution["validate_status"],
        "codex_config_evidence": plan["codex_config_evidence"],
        "effective_capacity": plan["effective_capacity"],
        "open_workers_at_plan_time": plan["open_workers"],
        "available_slots_at_plan_time": plan["available_slots"],
        "waves": plan["waves"],
        "ready_task_ids": plan["ready_task_ids"],
        "executed_task_ids": executed_task_ids,
        "not_executed_ready_task_ids": not_executed,
        "superseded_worker_ids": plan["superseded_worker_ids"],
        "workers": workers,
        "dispatch_counts": dispatch_counts,
        "communication": execution["communication"],
        "unknown_write_path_task_ids": unknown_write_tasks,
        "active_workers_after_execution": len(execution["active_worker_ids_after_execution"]),
        "active_worker_ids_after_execution": execution["active_worker_ids_after_execution"],
        "writes_observed": execution["writes_observed"],
    }


def build_execution_digest(
    execution_entries: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    roles: RoleRegistry,
    codex_config: dict[str, Any],
) -> dict[str, Any]:
    entries = list(execution_entries)
    if not entries:
        raise PlanError("execution digest requires at least one plan/execution entry")
    plan_ids: set[str] = set()
    attempt_ids: set[str] = set()
    superseded_worker_ids: set[str] = set()
    active_worker_ids: set[str] = set()
    not_executed_candidates: set[str] = set()
    unknown_write_tasks: set[str] = set()
    runtime_status_counts: dict[str, int] = defaultdict(int)
    task_outcome_counts: dict[str, int] = defaultdict(int)
    role_stats: dict[str, dict[str, Any]] = {}
    non_accepted_attempts: list[dict[str, str]] = []
    unevaluated_attempts: list[dict[str, str]] = []
    writes_counts = {"observed_true": 0, "observed_false": 0, "unknown": 0}
    dispatch_totals = {
        "fresh_spawn_count": 0,
        "reused_followup_count": 0,
        "isolated_fork_count": 0,
        "parent_followup_count": 0,
        "worker_intermediate_message_count": 0,
        "wait_call_count": 0,
        "wait_timeout_count": 0,
        "status_poll_count": 0,
    }
    accepted_attempt_count = 0
    independent_review_count = 0

    for plan_payload, execution_payload in entries:
        summary = build_execution_summary(
            plan_payload, execution_payload, roles, codex_config
        )
        plan_id = summary["plan_id"]
        if plan_id in plan_ids:
            raise PlanError(f"duplicate plan_id in digest: {plan_id}")
        plan_ids.add(plan_id)
        superseded_worker_ids.update(summary["superseded_worker_ids"])
        active_worker_ids = set(summary["active_worker_ids_after_execution"])
        not_executed_candidates.update(summary["not_executed_ready_task_ids"])
        unknown_write_tasks.update(summary["unknown_write_path_task_ids"])
        writes = summary["writes_observed"]
        if writes is True:
            writes_counts["observed_true"] += 1
        elif writes is False:
            writes_counts["observed_false"] += 1
        else:
            writes_counts["unknown"] += 1
        for key in ("fresh_spawn_count", "reused_followup_count", "isolated_fork_count"):
            dispatch_totals[key] += summary["dispatch_counts"][key]
        for key in ("wait_call_count", "wait_timeout_count", "status_poll_count"):
            dispatch_totals[key] += summary["communication"][key]

        for worker in summary["workers"]:
            task_id = worker["task_id"]
            if task_id in attempt_ids:
                raise PlanError(f"duplicate executed task_id in digest: {task_id}")
            attempt_ids.add(task_id)
            dispatch_totals["parent_followup_count"] += worker["parent_followup_count"]
            dispatch_totals["worker_intermediate_message_count"] += worker[
                "worker_intermediate_message_count"
            ]
            runtime_status = worker["final_status"]
            outcome = worker["task_outcome"]
            runtime_status_counts[runtime_status] += 1
            task_outcome_counts[outcome] += 1
            if outcome == "accepted":
                accepted_attempt_count += 1
            elif outcome == "not_evaluated":
                unevaluated_attempts.append(
                    {"task_id": task_id, "runtime_status": runtime_status}
                )
            else:
                non_accepted_attempts.append(
                    {
                        "task_id": task_id,
                        "task_outcome": outcome,
                        "runtime_status": runtime_status,
                    }
                )
            if worker["independent_review"]:
                independent_review_count += 1

            agent_type = worker["agent_type"]
            signature = (
                worker["configured_model"],
                worker["configured_model_reasoning_effort"],
                worker["configured_sandbox_mode"],
                worker["profile_file"],
                worker["profile_sha256"],
            )
            stats = role_stats.get(agent_type)
            if stats is None:
                stats = {
                    "agent_type": agent_type,
                    "configured_model": worker["configured_model"],
                    "configured_model_reasoning_effort": worker[
                        "configured_model_reasoning_effort"
                    ],
                    "configured_sandbox_mode": worker["configured_sandbox_mode"],
                    "profile_source": worker["profile_source"],
                    "profile_file": worker["profile_file"],
                    "profile_sha256": worker["profile_sha256"],
                    "executed_attempt_count": 0,
                    "accepted_attempt_count": 0,
                    "independent_review_count": 0,
                    "runtime_status_counts": defaultdict(int),
                    "task_outcome_counts": defaultdict(int),
                    "_signature": signature,
                }
                role_stats[agent_type] = stats
            elif stats["_signature"] != signature:
                raise PlanError(f"conflicting role profile in digest: {agent_type}")
            stats["executed_attempt_count"] += 1
            stats["runtime_status_counts"][runtime_status] += 1
            stats["task_outcome_counts"][outcome] += 1
            if outcome == "accepted":
                stats["accepted_attempt_count"] += 1
            if worker["independent_review"]:
                stats["independent_review_count"] += 1

    agent_profiles: list[dict[str, Any]] = []
    for agent_type in sorted(role_stats):
        row = role_stats[agent_type]
        row.pop("_signature")
        row["runtime_status_counts"] = dict(sorted(row["runtime_status_counts"].items()))
        row["task_outcome_counts"] = dict(sorted(row["task_outcome_counts"].items()))
        agent_profiles.append(row)

    not_executed_ready = not_executed_candidates - attempt_ids
    anomalies: list[dict[str, Any]] = []
    if non_accepted_attempts:
        anomalies.append({"type": "non_accepted_attempts", "attempts": non_accepted_attempts})
    if unevaluated_attempts:
        anomalies.append({"type": "unevaluated_attempts", "attempts": unevaluated_attempts})
    if not_executed_ready:
        anomalies.append(
            {"type": "not_executed_ready_tasks", "task_ids": sorted(not_executed_ready)}
        )
    if active_worker_ids:
        anomalies.append(
            {"type": "active_workers_after_execution", "worker_ids": sorted(active_worker_ids)}
        )
    if writes_counts["unknown"] or unknown_write_tasks:
        anomalies.append(
            {
                "type": "unknown_write_evidence",
                "plan_count": writes_counts["unknown"],
                "task_ids": sorted(unknown_write_tasks),
            }
        )

    return {
        "digest_type": "SUBAGENT_EXECUTION_DIGEST",
        "digest_version": DIGEST_VERSION,
        "profile_evidence": {"source": "agent_toml", "runtime_verified": False},
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
        "dispatch_and_communication": dispatch_totals,
        "not_executed_ready_task_ids": sorted(not_executed_ready),
        "superseded_worker_ids": sorted(superseded_worker_ids),
        "active_worker_ids_after_execution": sorted(active_worker_ids),
        "unknown_write_path_task_ids": sorted(unknown_write_tasks),
        "writes_observed": writes_counts,
        "anomalies": anomalies,
    }


def build_doctor_report(roles: RoleRegistry, codex_config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    raw = codex_config.get("raw", {})
    agents = raw.get("agents", {}) if isinstance(raw, dict) else {}
    features = raw.get("features", {}) if isinstance(raw, dict) else {}
    v2 = features.get("multi_agent_v2", {}) if isinstance(features, dict) else {}
    if not codex_config.get("exists"):
        errors.append("Codex config.toml 不存在，无法核对并发和 multi_agent_v2 配置。")
    if not isinstance(codex_config.get("configured_max_concurrent_threads_per_session"), int):
        errors.append("[agents].max_concurrent_threads_per_session 必须显式配置。")
    expected = {
        "enabled": True,
        "hide_spawn_agent_metadata": False,
        "expose_spawn_agent_model_overrides": False,
        "tool_namespace": "agents",
        "wait_agent_enabled": True,
        "non_code_mode_only": True,
    }
    if not isinstance(v2, dict):
        errors.append("[features.multi_agent_v2] 必须是 TOML table。")
        v2 = {}
    for key, expected_value in expected.items():
        actual = v2.get(key)
        if actual != expected_value:
            errors.append(
                f"features.multi_agent_v2.{key} 应为 {expected_value!r}，当前为 {actual!r}。"
            )
    timeout_keys = ["min_wait_timeout_ms", "default_wait_timeout_ms", "max_wait_timeout_ms"]
    timeouts: list[int] = []
    for key in timeout_keys:
        value = v2.get(key)
        if type(value) is not int or value < 1:
            errors.append(f"features.multi_agent_v2.{key} 必须是正整数。")
        else:
            timeouts.append(value)
    if len(timeouts) == 3 and not (timeouts[0] <= timeouts[1] <= timeouts[2]):
        errors.append("wait timeout 必须满足 min <= default <= max。")
    if isinstance(agents, dict):
        default_model = agents.get("default_subagent_model")
        default_effort = agents.get("default_subagent_reasoning_effort")
        if not isinstance(default_model, str) or not default_model.strip():
            warnings.append("[agents].default_subagent_model 未显式配置，仅影响未命名回退角色。")
        if not isinstance(default_effort, str) or not default_effort.strip():
            warnings.append(
                "[agents].default_subagent_reasoning_effort 未显式配置，仅影响未命名回退角色。"
            )
    return {
        "report_type": "MULTI_AGENT_ORCHESTRATION_DOCTOR",
        "report_version": DOCTOR_VERSION,
        "planner_version": PLANNER_VERSION,
        "status": "passed" if not errors else "failed",
        "role_count": len(roles),
        "roles": [role_profile_evidence(roles[name]) for name in sorted(roles)],
        "codex_config": {
            "config_file": codex_config.get("config_file"),
            "config_sha256": codex_config.get("config_sha256"),
            "configured_max_concurrent_threads_per_session": codex_config.get(
                "configured_max_concurrent_threads_per_session"
            ),
        },
        "errors": errors,
        "warnings": warnings,
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
        f"plan_id: {summary['plan_id']}",
        f"validate_status: {summary['validate_status']}",
        f"plan_command: {_json_inline(summary['plan_command'])}",
        f"validate_command: {_json_inline(summary['validate_command'])}",
        f"codex_config_evidence: {_json_inline(summary['codex_config_evidence'])}",
        f"effective_capacity: {summary['effective_capacity']}",
        f"open_workers_at_plan_time: {summary['open_workers_at_plan_time']}",
        f"available_slots_at_plan_time: {summary['available_slots_at_plan_time']}",
        f"waves: {_json_inline(summary['waves'])}",
        f"ready_task_ids: {_json_inline(summary['ready_task_ids'])}",
        f"executed_task_ids: {_json_inline(summary['executed_task_ids'])}",
        f"not_executed_ready_task_ids: {_json_inline(summary['not_executed_ready_task_ids'])}",
        f"superseded_worker_ids: {_json_inline(summary['superseded_worker_ids'])}",
        "workers:",
    ]
    for worker in summary["workers"]:
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
                f"    profile_file: {_json_inline(worker['profile_file'])}",
                f"    profile_sha256: {worker['profile_sha256']}",
                f"    worker_id: {worker['worker_id']}",
                "    runtime_ref: "
                + ("unknown" if worker["runtime_ref"] is None else _json_inline(worker["runtime_ref"])),
                f"    runtime_ref_source: {worker['runtime_ref_source']}",
                f"    observed_dispatch: {_json_inline(worker['observed_dispatch'])}",
                f"    planned_write_paths: {_json_inline(worker['planned_write_paths'])}",
                f"    observed_write_paths: {_json_inline(worker['observed_write_paths'])}",
                f"    final_status: {worker['final_status']}",
                f"    task_outcome: {worker['task_outcome']}",
                f"    active_after_close: {str(worker['active_after_close']).lower()}",
                f"    retired_from_followup: {str(worker['retired_from_followup']).lower()}",
                f"    retirement_source: {worker['retirement_source']}",
                f"    parent_followup_count: {worker['parent_followup_count']}",
                "    worker_intermediate_message_count: "
                + str(worker["worker_intermediate_message_count"]),
            ]
        )
    if not summary["workers"]:
        lines.append("  []")
    lines.extend(
        [
            f"dispatch_counts: {_json_inline(summary['dispatch_counts'])}",
            f"communication: {_json_inline(summary['communication'])}",
            "unknown_write_path_task_ids: "
            + _json_inline(summary["unknown_write_path_task_ids"]),
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
    dispatch = digest["dispatch_and_communication"]
    lines.extend(
        [
            "",
            (
                f"计划校验：{digest['validated_plan_count']}/{digest['plan_count']} 通过；"
                f"执行尝试：{digest['accepted_attempt_count']}/{digest['executed_attempt_count']} 验收通过；"
                f"独立复核：{digest['independent_review_count']}；"
                f"fresh 派发：{dispatch['fresh_spawn_count']}；"
                f"Worker 复用：{dispatch['reused_followup_count']}；"
                f"隔离上下文派发：{dispatch['isolated_fork_count']}；"
                f"父代理 follow-up：{dispatch['parent_followup_count']}；"
                f"Worker 中途消息：{dispatch['worker_intermediate_message_count']}；"
                f"等待调用/超时：{dispatch['wait_call_count']}/{dispatch['wait_timeout_count']}；"
                f"状态轮询：{dispatch['status_poll_count']}；"
                f"未执行 ready task：{len(digest['not_executed_ready_task_ids'])}；"
                f"残留活跃 Worker：{len(digest['active_worker_ids_after_execution'])}；"
                f"写入观测：{writes['observed_true']} 有写入、"
                f"{writes['observed_false']} 无写入、{writes['unknown']} 未知。"
            ),
        ]
    )
    if digest["anomalies"]:
        lines.extend(["", "**异常：**"])
        for anomaly in digest["anomalies"]:
            kind = anomaly["type"]
            if kind == "non_accepted_attempts":
                value = ", ".join(
                    f"{item['task_id']}={item['task_outcome']} (runtime={item['runtime_status']})"
                    for item in anomaly["attempts"]
                )
                lines.append(f"- 未验收通过的执行尝试：{value}")
            elif kind == "unevaluated_attempts":
                value = ", ".join(
                    f"{item['task_id']} (runtime={item['runtime_status']})"
                    for item in anomaly["attempts"]
                )
                lines.append(f"- 尚未验收的执行尝试：{value}")
            elif kind == "not_executed_ready_tasks":
                lines.append("- 未执行 ready task：" + ", ".join(anomaly["task_ids"]))
            elif kind == "active_workers_after_execution":
                lines.append("- 执行结束后仍活跃：" + ", ".join(anomaly["worker_ids"]))
            elif kind == "unknown_write_evidence":
                task_text = ", ".join(anomaly["task_ids"]) or "无具体 task_id"
                lines.append(
                    f"- 写入路径证据未知：{task_text}；批次数={anomaly['plan_count']}"
                )
    else:
        lines.extend(["", "**异常：** 无。"])
    lines.extend(
        [
            "",
            "模型和推理档位来自 Agent TOML 的固定配置快照，属于配置证据，"
            "不表示运行时接口已单独回报并验证这些值。",
            "验收通过仅按执行记录中的 `task_outcome=accepted` 统计；"
            "`final_status` 只表示 Worker 运行时状态。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_doctor(report: dict[str, Any]) -> str:
    lines = [
        "MULTI_AGENT_ORCHESTRATION_DOCTOR",
        f"status: {report['status']}",
        f"planner_version: {report['planner_version']}",
        f"role_count: {report['role_count']}",
        "codex_config: " + _json_inline(report["codex_config"]),
    ]
    if report["errors"]:
        lines.append("errors:")
        lines.extend(f"  - {item}" for item in report["errors"])
    else:
        lines.append("errors: []")
    if report["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  - {item}" for item in report["warnings"])
    else:
        lines.append("warnings: []")
    lines.append("roles:")
    for role in report["roles"]:
        lines.append(
            f"  - {role['name']}: {role['model']} / {role['model_reasoning_effort']} / "
            f"{role['sandbox_mode']} ({role['config_file']})"
        )
    return "\n".join(lines) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read input file: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return _require_object(value, "root")


def _write_text(text: str, path: Path | None) -> None:
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(value: dict[str, Any], path: Path | None) -> None:
    _write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=default_agents_dir(),
        help="Directory containing active fixed-profile Agent TOML files.",
    )
    parser.add_argument(
        "--codex-config",
        type=Path,
        default=default_codex_config(),
        help="Codex config.toml used for capacity and doctor checks.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="Validate a draft and assign waves.")
    plan_parser.add_argument("input", type=Path)
    plan_parser.add_argument("--output", type=Path)
    validate_parser = subparsers.add_parser(
        "validate", help="Recompute and validate a generated WorkPlan."
    )
    validate_parser.add_argument("input", type=Path)
    validate_parser.add_argument("--output", type=Path)
    summary_parser = subparsers.add_parser(
        "summary", help="Validate execution evidence and render a full audit summary."
    )
    summary_parser.add_argument("plan", type=Path)
    summary_parser.add_argument("execution", type=Path)
    summary_parser.add_argument("--output", type=Path)
    summary_parser.add_argument("--json", action="store_true")
    digest_parser = subparsers.add_parser(
        "digest", help="Aggregate chronological WorkPlan executions."
    )
    digest_parser.add_argument(
        "--entry",
        action="append",
        nargs=2,
        type=Path,
        required=True,
        metavar=("PLAN", "EXECUTION"),
    )
    digest_parser.add_argument("--output", type=Path)
    digest_parser.add_argument("--json", action="store_true")
    guard_parser = subparsers.add_parser(
        "guard-dispatch",
        help="Validate a normalized spawn_agent/followup_task payload before dispatch.",
    )
    guard_parser.add_argument("plan", type=Path)
    guard_parser.add_argument("task_id")
    guard_parser.add_argument("dispatch", type=Path)
    guard_parser.add_argument("--output", type=Path)
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check fixed-profile Agent TOMLs and Codex runtime configuration."
    )
    doctor_parser.add_argument("--output", type=Path)
    doctor_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        roles = load_roles(args.agents_dir)
        codex_config = load_codex_config(
            args.codex_config, required=args.command == "doctor"
        )
        if args.command == "plan":
            _write_json(canonical_plan(_read_json(args.input), roles, codex_config), args.output)
        elif args.command == "validate":
            _write_json(
                validate_generated_plan(_read_json(args.input), roles, codex_config),
                args.output,
            )
        elif args.command == "summary":
            summary = build_execution_summary(
                _read_json(args.plan), _read_json(args.execution), roles, codex_config
            )
            if args.json:
                _write_json(summary, args.output)
            else:
                _write_text(render_execution_summary(summary), args.output)
        elif args.command == "digest":
            digest = build_execution_digest(
                [(_read_json(plan), _read_json(execution)) for plan, execution in args.entry],
                roles,
                codex_config,
            )
            if args.json:
                _write_json(digest, args.output)
            else:
                _write_text(render_execution_digest(digest), args.output)
        elif args.command == "guard-dispatch":
            result = guard_dispatch(
                _read_json(args.plan),
                args.task_id,
                _read_json(args.dispatch),
                roles,
                codex_config,
            )
            _write_json(result, args.output)
        else:
            report = build_doctor_report(roles, codex_config)
            if args.json:
                _write_json(report, args.output)
            else:
                _write_text(render_doctor(report), args.output)
            return 0 if report["status"] == "passed" else 2
        return 0
    except (PlanError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
