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
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, TypedDict

SKILL_RELEASE = "1.6.0"
PLANNER_VERSION = "1.5.1"
SCHEMA_VERSION = 5
EXECUTION_RECORD_VERSION = 6
SUMMARY_VERSION = 3
DIGEST_VERSION = 3
DOCTOR_VERSION = 2
AUDIT_BUNDLE_VERSION = 1
DEFAULT_AUDIT_RETENTION_DAYS = 14
HIGH_RISK_AUDIT_RETENTION_DAYS = 90

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
DIGEST_RENDER_COUNT_FIELDS = {
    "plan_count",
    "validated_plan_count",
    "executed_attempt_count",
    "accepted_attempt_count",
    "independent_review_count",
}
DIGEST_RENDER_PROFILE_FIELDS = {
    "agent_type",
    "configured_model",
    "configured_model_reasoning_effort",
    "executed_attempt_count",
    "accepted_attempt_count",
    "independent_review_count",
}
DIGEST_RENDER_DISPATCH_FIELDS = {
    "fresh_spawn_count",
    "reused_followup_count",
    "isolated_fork_count",
    "parent_followup_count",
    "worker_intermediate_message_count",
    "wait_call_count",
    "wait_timeout_count",
    "status_poll_count",
}
DIGEST_RENDER_WRITE_FIELDS = {"observed_true", "observed_false", "unknown"}
DIGEST_RENDER_ANOMALY_TYPES = {
    "non_accepted_attempts",
    "unevaluated_attempts",
    "not_executed_ready_tasks",
    "active_workers_after_execution",
    "unknown_write_evidence",
}


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


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise PlanError(f"cannot hash file {path}: {exc}") from exc


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


def guard_audit_membership(
    bundle: Path,
    plan_path: Path,
    dispatch_path: Path,
) -> dict[str, Any]:
    """Require the guarded dispatch to use files allocated by one open audit stage.

    The hook adapter writes the normalized dispatch payload into the stage's
    persistent dispatch directory before invoking ``guard-dispatch``. This
    closes the gap where a compliant tool call could execute while its plan or
    dispatch evidence existed only in a transient location.
    """
    bundle = bundle.expanduser().resolve()
    manifest = _read_audit_manifest(bundle)
    if manifest.get("status") != "open":
        raise PlanError("guard audit bundle must be open before dispatch")

    raw_plan = plan_path.expanduser()
    raw_dispatch = dispatch_path.expanduser()
    for value, label in ((raw_plan, "plan"), (raw_dispatch, "dispatch")):
        if value.is_symlink():
            raise PlanError(f"guard audit {label} must not be a symlink")
        if not value.is_file():
            raise PlanError(f"guard audit {label} file does not exist: {value}")

    resolved_plan = raw_plan.resolve()
    resolved_dispatch = raw_dispatch.resolve()
    if not resolved_plan.is_relative_to(bundle):
        raise PlanError("guard audit plan must be inside the persistent audit bundle")
    if not resolved_dispatch.is_relative_to(bundle):
        raise PlanError("guard audit dispatch must be inside the persistent audit bundle")

    stages = manifest.get("stages")
    if not isinstance(stages, list):
        raise PlanError("guard audit manifest stages must be a list")
    matches: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        paths = stage.get("paths")
        if not isinstance(paths, dict):
            continue
        planned_path = paths.get("plan")
        dispatch_dir = paths.get("dispatch_dir")
        if not isinstance(planned_path, str) or not isinstance(dispatch_dir, str):
            continue
        stage_plan = (bundle / planned_path).resolve()
        stage_dispatch_dir = (bundle / dispatch_dir).resolve()
        if (
            resolved_plan == stage_plan
            and resolved_dispatch.is_relative_to(stage_dispatch_dir)
            and resolved_dispatch.parent == stage_dispatch_dir
        ):
            matches.append(stage)

    if not matches:
        raise PlanError(
            "guard audit plan and dispatch are not allocated to the same audit stage"
        )
    if len(matches) > 1:
        raise PlanError("guard audit membership is ambiguous across multiple stages")
    stage = matches[0]
    plan_value = _read_json(resolved_plan)
    plan_id = plan_value.get("plan_id")
    stage_plan_id = stage.get("plan_id")
    if stage_plan_id not in (None, plan_id):
        raise PlanError("guard audit stage plan_id does not match the generated plan")

    return {
        "audit_id": manifest.get("audit_id"),
        "audit_bundle_version": manifest.get("bundle_version"),
        "audit_stage": stage.get("prefix"),
        "audit_plan": resolved_plan.relative_to(bundle).as_posix(),
        "audit_dispatch": resolved_dispatch.relative_to(bundle).as_posix(),
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


def validate_digest_snapshot_for_render(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an already-built Digest before pure Markdown rendering.

    Rendering an archived Digest must not re-read current Agent TOMLs or
    ``config.toml``. Those live files may legitimately drift after the Digest
    was produced, while the validated JSON snapshot remains the authoritative
    historical audit artifact.
    """
    root = _require_object(payload, "digest")
    if root.get("digest_type") != "SUBAGENT_EXECUTION_DIGEST":
        raise PlanError(
            "digest.digest_type must equal SUBAGENT_EXECUTION_DIGEST"
        )
    if root.get("digest_version") != DIGEST_VERSION:
        raise PlanError(f"digest.digest_version must equal {DIGEST_VERSION}")

    for key in sorted(DIGEST_RENDER_COUNT_FIELDS):
        _require_nonnegative_int(root.get(key), f"digest.{key}")
    if root["validated_plan_count"] > root["plan_count"]:
        raise PlanError("digest.validated_plan_count exceeds plan_count")
    if root["accepted_attempt_count"] > root["executed_attempt_count"]:
        raise PlanError(
            "digest.accepted_attempt_count exceeds executed_attempt_count"
        )
    if root["independent_review_count"] > root["executed_attempt_count"]:
        raise PlanError(
            "digest.independent_review_count exceeds executed_attempt_count"
        )

    _string_list(
        root.get("plan_ids"),
        "digest.plan_ids",
        identifier_pattern=PLAN_ID_RE,
    )
    for key in (
        "not_executed_ready_task_ids",
        "superseded_worker_ids",
        "active_worker_ids_after_execution",
        "unknown_write_path_task_ids",
    ):
        value = root.get(key)
        pattern = WORKER_ID_RE if key in {
            "superseded_worker_ids",
            "active_worker_ids_after_execution",
        } else TASK_ID_RE
        _string_list(value, f"digest.{key}", identifier_pattern=pattern)

    profiles = root.get("agent_profiles")
    if not isinstance(profiles, list):
        raise PlanError("digest.agent_profiles must be a list")
    seen_roles: set[str] = set()
    for index, raw in enumerate(profiles):
        path = f"digest.agent_profiles[{index}]"
        row = _require_object(raw, path)
        missing = sorted(DIGEST_RENDER_PROFILE_FIELDS - set(row))
        if missing:
            raise PlanError(
                f"{path} is missing fields: {', '.join(missing)}"
            )
        role = _require_identifier(row.get("agent_type"), f"{path}.agent_type", ROLE_RE)
        if role in seen_roles:
            raise PlanError(f"digest.agent_profiles contains duplicate role: {role}")
        seen_roles.add(role)
        _require_nonempty_string(
            row.get("configured_model"), f"{path}.configured_model", 128
        )
        _require_nonempty_string(
            row.get("configured_model_reasoning_effort"),
            f"{path}.configured_model_reasoning_effort",
            64,
        )
        for key in (
            "executed_attempt_count",
            "accepted_attempt_count",
            "independent_review_count",
        ):
            _require_nonnegative_int(row.get(key), f"{path}.{key}")
        if row["accepted_attempt_count"] > row["executed_attempt_count"]:
            raise PlanError(f"{path}.accepted_attempt_count exceeds executed_attempt_count")
        if row["independent_review_count"] > row["executed_attempt_count"]:
            raise PlanError(
                f"{path}.independent_review_count exceeds executed_attempt_count"
            )

    dispatch = _require_object(
        root.get("dispatch_and_communication"),
        "digest.dispatch_and_communication",
    )
    for key in sorted(DIGEST_RENDER_DISPATCH_FIELDS):
        _require_nonnegative_int(
            dispatch.get(key), f"digest.dispatch_and_communication.{key}"
        )

    writes = _require_object(root.get("writes_observed"), "digest.writes_observed")
    for key in sorted(DIGEST_RENDER_WRITE_FIELDS):
        _require_nonnegative_int(writes.get(key), f"digest.writes_observed.{key}")

    anomalies = root.get("anomalies")
    if not isinstance(anomalies, list):
        raise PlanError("digest.anomalies must be a list")
    for index, raw in enumerate(anomalies):
        path = f"digest.anomalies[{index}]"
        row = _require_object(raw, path)
        kind = _require_nonempty_string(row.get("type"), f"{path}.type", 64)
        if kind not in DIGEST_RENDER_ANOMALY_TYPES:
            raise PlanError(f"{path}.type is unsupported: {kind}")
        if kind in {"non_accepted_attempts", "unevaluated_attempts"}:
            attempts = row.get("attempts")
            if not isinstance(attempts, list):
                raise PlanError(f"{path}.attempts must be a list")
            for attempt_index, raw_attempt in enumerate(attempts):
                attempt_path = f"{path}.attempts[{attempt_index}]"
                attempt = _require_object(raw_attempt, attempt_path)
                _require_identifier(
                    attempt.get("task_id"), f"{attempt_path}.task_id", TASK_ID_RE
                )
                _require_nonempty_string(
                    attempt.get("runtime_status"),
                    f"{attempt_path}.runtime_status",
                    32,
                )
                if kind == "non_accepted_attempts":
                    _require_nonempty_string(
                        attempt.get("task_outcome"),
                        f"{attempt_path}.task_outcome",
                        32,
                    )
        elif kind == "not_executed_ready_tasks":
            _string_list(
                row.get("task_ids"),
                f"{path}.task_ids",
                identifier_pattern=TASK_ID_RE,
            )
        elif kind == "active_workers_after_execution":
            _string_list(
                row.get("worker_ids"),
                f"{path}.worker_ids",
                identifier_pattern=WORKER_ID_RE,
            )
        else:
            _require_nonnegative_int(row.get("plan_count"), f"{path}.plan_count")
            _string_list(
                row.get("task_ids"),
                f"{path}.task_ids",
                identifier_pattern=TASK_ID_RE,
            )

    return root


# ---------------------------------------------------------------------------
# Persistent audit bundles
# ---------------------------------------------------------------------------


def default_audit_root() -> Path:
    explicit = os.environ.get("MULTI_AGENT_AUDIT_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "audits" / "multi-agent"
    return Path.home() / ".codex" / "audits" / "multi-agent"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_iso_z(value: Any, path: str) -> datetime:
    text = _require_nonempty_string(value, path, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanError(f"{path} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PlanError(f"{path} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _safe_slug(value: str, fallback: str, max_length: int = 48) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    normalized = re.sub(r"[-_.]{2,}", "-", normalized).strip("-._")
    if not normalized:
        normalized = fallback
    return normalized[:max_length].rstrip("-._") or fallback


def _repository_key(repo_root: Path) -> tuple[str, str]:
    resolved = repo_root.expanduser().resolve()
    name = resolved.name or "repo"
    slug = _safe_slug(name, "repo", 40)
    suffix = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{suffix}", name


def _retention_record(days: int | None, now: datetime) -> dict[str, Any]:
    if days is None:
        days = DEFAULT_AUDIT_RETENTION_DAYS
    if type(days) is not int or days < 0:
        raise PlanError("retention days must be a non-negative integer")
    keep = days == 0
    return {
        "days": days,
        "keep": keep,
        "delete_after": None if keep else _iso_z(now + timedelta(days=days)),
        "extended_for_attention": False,
    }


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _make_file_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_private_json(value: dict[str, Any], path: Path) -> None:
    _write_json(value, path)
    _make_file_private(path)


def _write_private_text(value: str, path: Path) -> None:
    _write_text(value, path)
    _make_file_private(path)


def _audit_manifest_path(bundle: Path) -> Path:
    return bundle / "manifest.json"


def _read_audit_manifest(bundle: Path) -> dict[str, Any]:
    path = _audit_manifest_path(bundle)
    if not path.is_file():
        raise PlanError(f"audit manifest does not exist: {path}")
    manifest = _read_json(path)
    if manifest.get("bundle_type") != "MULTI_AGENT_AUDIT_BUNDLE":
        raise PlanError("audit manifest bundle_type is invalid")
    if manifest.get("bundle_version") != AUDIT_BUNDLE_VERSION:
        raise PlanError(
            f"audit manifest bundle_version must equal {AUDIT_BUNDLE_VERSION}"
        )
    return manifest


def _audit_output_descriptor(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "audit_id": manifest["audit_id"],
        "audit_dir": str(bundle),
        "manifest": str(_audit_manifest_path(bundle)),
        "digest_json": str(bundle / "SUBAGENT_EXECUTION_DIGEST.json"),
        "digest_markdown": str(bundle / "SUBAGENT_EXECUTION_DIGEST.md"),
    }


def create_audit_bundle(
    audit_root: Path,
    repo_root: Path,
    task_name: str,
    *,
    risk: str = "normal",
    retention_days: int | None = None,
    keep: bool = False,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    task_name = _require_nonempty_string(task_name, "audit.task_name", 240)
    if risk not in {"normal", "high"}:
        raise PlanError("audit risk must be normal or high")
    current = now or _utc_now()
    root = audit_root.expanduser().resolve()
    repository = repo_root.expanduser().resolve()
    if not repository.is_dir():
        raise PlanError(f"repository root does not exist: {repository}")
    repo_key, repo_name = _repository_key(repository)
    task_slug = _safe_slug(
        task_name,
        "task-" + hashlib.sha256(task_name.encode("utf-8")).hexdigest()[:8],
    )
    timestamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    random_suffix = uuid.uuid4().hex[:8]
    audit_id = f"{timestamp}-{task_slug}-{random_suffix}"
    bundle = root / repo_key / audit_id
    if bundle.exists():
        raise PlanError(f"audit bundle already exists: {bundle}")
    for directory in (root, root / repo_key, bundle):
        _ensure_private_directory(directory)

    if keep:
        retention_days = 0
    elif retention_days is None:
        retention_days = (
            HIGH_RISK_AUDIT_RETENTION_DAYS
            if risk == "high"
            else DEFAULT_AUDIT_RETENTION_DAYS
        )
    manifest = {
        "bundle_type": "MULTI_AGENT_AUDIT_BUNDLE",
        "bundle_version": AUDIT_BUNDLE_VERSION,
        "audit_id": audit_id,
        "status": "open",
        "created_at": _iso_z(current),
        "updated_at": _iso_z(current),
        "closed_at": None,
        "skill_release": SKILL_RELEASE,
        "planner_version": PLANNER_VERSION,
        "repository": {
            "root": str(repository),
            "name": repo_name,
            "key": repo_key,
        },
        "task": {
            "name": task_name,
            "slug": task_slug,
            "risk": risk,
        },
        "retention": _retention_record(retention_days, current),
        "paths": {
            "digest_json": "SUBAGENT_EXECUTION_DIGEST.json",
            "digest_markdown": "SUBAGENT_EXECUTION_DIGEST.md",
        },
        "stages": [],
        "artifacts": [],
        "summary": None,
        "verification": {
            "status": "pending",
            "verified_at": None,
            "errors": [],
            "warnings": [],
        },
    }
    _write_private_json(manifest, _audit_manifest_path(bundle))
    return bundle, manifest


def allocate_audit_stage(bundle: Path, name: str) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    manifest = _read_audit_manifest(bundle)
    if manifest.get("status") != "open":
        raise PlanError("audit stages can only be allocated while the bundle is open")
    display_name = _require_nonempty_string(name, "audit.stage_name", 120)
    slug = _safe_slug(
        display_name,
        "stage-" + hashlib.sha256(display_name.encode("utf-8")).hexdigest()[:8],
        40,
    )
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        raise PlanError("audit manifest stages must be a list")
    sequence = len(stages) + 1
    prefix = f"{sequence:02d}-{slug}"
    if any(stage.get("prefix") == prefix for stage in stages if isinstance(stage, dict)):
        raise PlanError(f"audit stage already exists: {prefix}")
    dispatch_dir = bundle / f"{prefix}.dispatches"
    _ensure_private_directory(dispatch_dir)
    relative_paths = {
        "draft": f"{prefix}.draft.json",
        "plan": f"{prefix}.plan.json",
        "dispatch_dir": f"{prefix}.dispatches",
        "execution": f"{prefix}.execution.json",
        "summary_json": f"{prefix}.summary.json",
        "summary_text": f"{prefix}.summary.txt",
    }
    stage = {
        "sequence": sequence,
        "name": display_name,
        "slug": slug,
        "prefix": prefix,
        "plan_id": None,
        "paths": relative_paths,
    }
    stages.append(stage)
    manifest["updated_at"] = _iso_z(_utc_now())
    _write_private_json(manifest, _audit_manifest_path(bundle))
    return {
        "audit_id": manifest["audit_id"],
        "audit_dir": str(bundle),
        "sequence": sequence,
        "name": display_name,
        "prefix": prefix,
        "paths": {
            key: str(bundle / value) for key, value in relative_paths.items()
        },
    }


def _read_json_loose(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot parse JSON artifact {path}: {exc}") from exc


def _classify_audit_file(path: Path) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if path.suffix == ".json":
        value = _read_json_loose(path)
        if not isinstance(value, dict):
            return "unknown_json", metadata
        if value.get("digest_type") == "SUBAGENT_EXECUTION_DIGEST":
            metadata["schema_version"] = value.get("digest_version")
            metadata["plan_ids"] = value.get("plan_ids")
            return "digest_json", metadata
        if value.get("summary_type") == "WORKPLAN_EXECUTION_SUMMARY":
            metadata["schema_version"] = value.get("summary_version")
            metadata["plan_id"] = value.get("plan_id")
            return "summary_json", metadata
        if (
            value.get("version") == EXECUTION_RECORD_VERSION
            and isinstance(value.get("workers"), list)
            and isinstance(value.get("communication"), dict)
        ):
            metadata["schema_version"] = value.get("version")
            metadata["plan_id"] = value.get("plan_id")
            return "execution_json", metadata
        if "planner_version" in value and "ready_task_ids" in value:
            metadata["schema_version"] = value.get("version")
            metadata["planner_version"] = value.get("planner_version")
            metadata["plan_id"] = value.get("plan_id")
            return "generated_plan_json", metadata
        if "max_concurrent_workers" in value and "tasks" in value:
            metadata["schema_version"] = value.get("version")
            metadata["plan_id"] = value.get("plan_id")
            return "draft_plan_json", metadata
        if all(
            key in value
            for key in ("method", "task_name", "agent_type", "fork_turns")
        ):
            metadata["task_name"] = value.get("task_name")
            metadata["agent_type"] = value.get("agent_type")
            return "dispatch_json", metadata
        return "unknown_json", metadata
    if path.suffix == ".md":
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PlanError(f"cannot read Markdown artifact {path}: {exc}") from exc
        if text.startswith("### 子任务执行概览\n"):
            return "digest_markdown", metadata
        return "unknown_markdown", metadata
    if path.suffix == ".txt":
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PlanError(f"cannot read text artifact {path}: {exc}") from exc
        if text.startswith("WORKPLAN_EXECUTION_SUMMARY\n"):
            return "summary_text", metadata
        return "unknown_text", metadata
    return "unknown_file", metadata


def _artifact_record(bundle: Path, path: Path) -> dict[str, Any]:
    kind, metadata = _classify_audit_file(path)
    record = {
        "path": path.relative_to(bundle).as_posix(),
        "kind": kind,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }
    record.update({key: value for key, value in metadata.items() if value is not None})
    return record


def _scan_audit_artifacts(bundle: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    manifest = _audit_manifest_path(bundle)
    for path in sorted(bundle.rglob("*")):
        if path == manifest or path.name.startswith("."):
            continue
        if path.is_symlink():
            errors.append(f"audit bundle contains symlink: {path.relative_to(bundle)}")
            continue
        if not path.is_file():
            continue
        try:
            records.append(_artifact_record(bundle, path))
        except (PlanError, OSError) as exc:
            errors.append(str(exc))
    return records, errors


def _single_by_plan_id(
    records: list[dict[str, Any]], kind: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["kind"] != kind:
            continue
        plan_id = record.get("plan_id")
        if not isinstance(plan_id, str):
            errors.append(f"{record['path']} is missing plan_id")
            continue
        if plan_id in result:
            errors.append(f"duplicate {kind} for plan_id={plan_id}")
            continue
        result[plan_id] = record
    return result


def _dispatch_signature_from_file(path: Path) -> tuple[Any, ...]:
    value = _read_json_loose(path)
    if not isinstance(value, dict):
        raise PlanError(f"dispatch artifact must be an object: {path}")
    return (
        value.get("method"),
        value.get("task_name"),
        value.get("agent_type"),
        value.get("fork_turns"),
        value.get("model"),
        value.get("reasoning_effort"),
    )


def _dispatch_signature_from_worker(worker: dict[str, Any]) -> tuple[Any, ...]:
    observed = worker.get("observed_dispatch")
    if not isinstance(observed, dict):
        return (None, None, worker.get("agent_type"), None, None, None)
    return (
        observed.get("method"),
        observed.get("task_name"),
        worker.get("agent_type"),
        observed.get("fork_turns"),
        observed.get("model_override"),
        observed.get("reasoning_effort_override"),
    )


def inspect_audit_bundle(
    bundle: Path,
    *,
    compare_manifest: bool,
) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    manifest = _read_audit_manifest(bundle)
    errors: list[str] = []
    warnings: list[str] = []
    records, scan_errors = _scan_audit_artifacts(bundle)
    errors.extend(scan_errors)

    drafts = _single_by_plan_id(records, "draft_plan_json", errors)
    plans = _single_by_plan_id(records, "generated_plan_json", errors)
    executions = _single_by_plan_id(records, "execution_json", errors)
    summaries = _single_by_plan_id(records, "summary_json", errors)

    if not plans:
        errors.append("audit bundle must contain at least one generated WorkPlan")
    plan_ids = set(plans)
    if set(drafts) != plan_ids:
        errors.append(
            "draft/generated plan_id sets differ: "
            f"drafts={sorted(drafts)}, plans={sorted(plan_ids)}"
        )
    if set(executions) != plan_ids:
        errors.append(
            "plan/execution plan_id sets differ: "
            f"plans={sorted(plan_ids)}, executions={sorted(executions)}"
        )
    if set(summaries) != plan_ids:
        errors.append(
            "plan/summary plan_id sets differ: "
            f"plans={sorted(plan_ids)}, summaries={sorted(summaries)}"
        )

    digest_records = [record for record in records if record["kind"] == "digest_json"]
    digest_markdown_records = [
        record for record in records if record["kind"] == "digest_markdown"
    ]
    if len(digest_records) != 1:
        errors.append(
            f"audit bundle must contain exactly one Digest JSON; found {len(digest_records)}"
        )
        digest: dict[str, Any] | None = None
    else:
        digest_path = bundle / digest_records[0]["path"]
        digest = validate_digest_snapshot_for_render(_read_json(digest_path))
        digest_plan_ids = set(digest.get("plan_ids", []))
        if digest_plan_ids != plan_ids:
            errors.append(
                "Digest plan_ids do not match generated plans: "
                f"digest={sorted(digest_plan_ids)}, plans={sorted(plan_ids)}"
            )
        if digest.get("plan_count") != len(plan_ids):
            errors.append("Digest plan_count does not match generated plans")
        if digest.get("validated_plan_count") != len(plan_ids):
            errors.append("Digest validated_plan_count does not match generated plans")
    if len(digest_markdown_records) != 1:
        errors.append(
            "audit bundle must contain exactly one Digest Markdown; "
            f"found {len(digest_markdown_records)}"
        )
    elif digest is not None:
        markdown_path = bundle / digest_markdown_records[0]["path"]
        actual_markdown = markdown_path.read_text(encoding="utf-8")
        expected_markdown = render_execution_digest(digest)
        if actual_markdown != expected_markdown:
            errors.append("Digest Markdown does not match deterministic renderer output")

    execution_workers: list[dict[str, Any]] = []
    accepted_attempt_count = 0
    independent_review_count = 0
    for plan_id, record in executions.items():
        value = _read_json(bundle / record["path"])
        workers = value.get("workers")
        if isinstance(workers, list):
            execution_workers.extend(worker for worker in workers if isinstance(worker, dict))
            accepted_attempt_count += sum(
                worker.get("task_outcome") == "accepted"
                for worker in workers
                if isinstance(worker, dict)
            )
        summary_record = summaries.get(plan_id)
        if summary_record is not None:
            summary_value = _read_json(bundle / summary_record["path"])
            independent_review_count += sum(
                worker.get("independent_review") is True
                for worker in summary_value.get("workers", [])
                if isinstance(worker, dict)
            )

    dispatch_paths = [
        bundle / record["path"] for record in records if record["kind"] == "dispatch_json"
    ]
    try:
        dispatch_signatures = Counter(
            _dispatch_signature_from_file(path) for path in dispatch_paths
        )
    except PlanError as exc:
        errors.append(str(exc))
        dispatch_signatures = Counter()
    worker_signatures = Counter(
        _dispatch_signature_from_worker(worker) for worker in execution_workers
    )
    if dispatch_signatures != worker_signatures:
        errors.append(
            "dispatch artifacts do not match execution observed_dispatch records"
        )

    if digest is not None:
        if digest.get("executed_attempt_count") != len(execution_workers):
            errors.append("Digest executed_attempt_count does not match execution records")
        if digest.get("accepted_attempt_count") != accepted_attempt_count:
            errors.append("Digest accepted_attempt_count does not match execution records")
        if digest.get("independent_review_count") != independent_review_count:
            errors.append("Digest independent_review_count does not match summaries")

    unknown_records = [
        record
        for record in records
        if record["kind"].startswith("unknown_") or record["kind"] == "unknown_file"
    ]
    for record in unknown_records:
        warnings.append(f"unclassified audit artifact: {record['path']}")

    stages = manifest.get("stages")
    if not isinstance(stages, list):
        errors.append("audit manifest stages must be a list")
        stages = []
    if not stages:
        warnings.append("audit bundle has no allocated stages")
    for stage in stages:
        if not isinstance(stage, dict):
            errors.append("audit manifest contains a non-object stage")
            continue
        paths = stage.get("paths")
        if not isinstance(paths, dict):
            errors.append(f"audit stage {stage.get('prefix')} paths must be an object")
            continue
        required = {"draft", "plan", "dispatch_dir", "execution", "summary_json"}
        missing = sorted(required - set(paths))
        if missing:
            errors.append(
                f"audit stage {stage.get('prefix')} is missing paths: {', '.join(missing)}"
            )
            continue
        for key in ("draft", "plan", "execution", "summary_json"):
            if not (bundle / str(paths[key])).is_file():
                errors.append(
                    f"audit stage {stage.get('prefix')} is missing {key}: {paths[key]}"
                )
        dispatch_dir = bundle / str(paths["dispatch_dir"])
        if not dispatch_dir.is_dir():
            errors.append(
                f"audit stage {stage.get('prefix')} is missing dispatch_dir: {paths['dispatch_dir']}"
            )
        plan_path = bundle / str(paths["plan"])
        if plan_path.is_file():
            plan_value = _read_json(plan_path)
            plan_id = plan_value.get("plan_id")
            if stage.get("plan_id") not in (None, plan_id):
                errors.append(
                    f"audit stage {stage.get('prefix')} plan_id does not match its plan file"
                )

    size_bytes = sum(record["size_bytes"] for record in records)
    attention = bool(digest and digest.get("anomalies"))
    summary = {
        "plan_count": len(plan_ids),
        "executed_attempt_count": len(execution_workers),
        "accepted_attempt_count": accepted_attempt_count,
        "independent_review_count": independent_review_count,
        "attention_required": attention,
        "anomaly_count": len(digest.get("anomalies", [])) if digest else 0,
    }

    if compare_manifest:
        manifest_records = manifest.get("artifacts")
        if not isinstance(manifest_records, list):
            errors.append("closed audit manifest artifacts must be a list")
        else:
            expected_by_path = {
                record.get("path"): record
                for record in manifest_records
                if isinstance(record, dict) and isinstance(record.get("path"), str)
            }
            actual_by_path = {record["path"]: record for record in records}
            if set(expected_by_path) != set(actual_by_path):
                errors.append("manifest artifact inventory does not match files on disk")
            for path, actual in actual_by_path.items():
                expected = expected_by_path.get(path)
                if expected is None:
                    continue
                if expected.get("sha256") != actual["sha256"]:
                    errors.append(f"artifact checksum mismatch: {path}")
                if expected.get("size_bytes") != actual["size_bytes"]:
                    errors.append(f"artifact size mismatch: {path}")
        if manifest.get("status") != "closed":
            errors.append("audit bundle is not closed")
        verification = manifest.get("verification")
        if not isinstance(verification, dict) or verification.get("status") != "passed":
            errors.append("audit manifest verification status is not passed")

    return {
        "report_type": "MULTI_AGENT_AUDIT_BUNDLE_REPORT",
        "report_version": 1,
        "status": "passed" if not errors else "failed",
        "audit_id": manifest.get("audit_id"),
        "audit_dir": str(bundle),
        "bundle_status": manifest.get("status"),
        "artifact_count": len(records),
        "size_bytes": size_bytes,
        "summary": summary,
        "artifacts": records,
        "errors": errors,
        "warnings": warnings,
    }


def finalize_audit_bundle(
    bundle: Path,
    *,
    retention_days: int | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    manifest = _read_audit_manifest(bundle)
    if manifest.get("status") != "open":
        raise PlanError("only an open audit bundle can be finalized")

    digest_path = bundle / "SUBAGENT_EXECUTION_DIGEST.json"
    if digest_path.is_file():
        digest = validate_digest_snapshot_for_render(_read_json(digest_path))
        _write_private_text(
            render_execution_digest(digest),
            bundle / "SUBAGENT_EXECUTION_DIGEST.md",
        )

    for summary_path in sorted(bundle.rglob("*.json")):
        if summary_path.name == "manifest.json":
            continue
        try:
            summary = _read_json(summary_path)
        except PlanError:
            continue
        if summary.get("summary_type") == "WORKPLAN_EXECUTION_SUMMARY":
            _write_private_text(
                render_execution_summary(summary),
                summary_path.with_suffix(".txt"),
            )

    report = inspect_audit_bundle(bundle, compare_manifest=False)
    now = _utc_now()
    manifest["updated_at"] = _iso_z(now)
    manifest["verification"] = {
        "status": report["status"],
        "verified_at": _iso_z(now),
        "errors": report["errors"],
        "warnings": report["warnings"],
    }
    if report["status"] != "passed":
        _write_private_json(manifest, _audit_manifest_path(bundle))
        return report

    stages = manifest.get("stages", [])
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        paths = stage.get("paths", {})
        plan_path = bundle / str(paths.get("plan", ""))
        if plan_path.is_file():
            stage["plan_id"] = _read_json(plan_path).get("plan_id")

    current_retention = manifest.get("retention")
    if keep:
        retention_days = 0
    if retention_days is not None:
        manifest["retention"] = _retention_record(retention_days, now)
    elif not isinstance(current_retention, dict):
        manifest["retention"] = _retention_record(None, now)
    elif report["summary"]["attention_required"]:
        days = current_retention.get("days")
        if type(days) is int and 0 < days < HIGH_RISK_AUDIT_RETENTION_DAYS:
            manifest["retention"] = _retention_record(
                HIGH_RISK_AUDIT_RETENTION_DAYS, now
            )
            manifest["retention"]["extended_for_attention"] = True

    manifest["status"] = "closed"
    manifest["closed_at"] = _iso_z(now)
    manifest["updated_at"] = _iso_z(now)
    manifest["artifacts"] = report["artifacts"]
    manifest["summary"] = report["summary"]
    manifest["verification"] = {
        "status": "passed",
        "verified_at": _iso_z(now),
        "errors": [],
        "warnings": report["warnings"],
    }
    _write_private_json(manifest, _audit_manifest_path(bundle))
    for path in bundle.rglob("*"):
        if path.is_dir():
            _ensure_private_directory(path)
        elif path.is_file():
            _make_file_private(path)
    report["bundle_status"] = "closed"
    return report


def import_audit_directory(
    audit_root: Path,
    source: Path,
    repo_root: Path,
    task_name: str,
    *,
    risk: str = "normal",
    retention_days: int | None = None,
    keep: bool = False,
) -> tuple[Path, dict[str, Any]]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise PlanError(f"audit import source does not exist: {source}")
    root = audit_root.expanduser().resolve()
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise PlanError("audit import source and persistent audit root must not overlap")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise PlanError(f"audit import refuses symlink: {path.relative_to(source)}")
    bundle, _ = create_audit_bundle(
        audit_root,
        repo_root,
        task_name,
        risk=risk,
        retention_days=retention_days,
        keep=keep,
    )
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_dir():
            _ensure_private_directory(bundle / relative)
            continue
        if not path.is_file() or path.name == "manifest.json":
            continue
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        _make_file_private(target)
    report = finalize_audit_bundle(bundle)
    return bundle, report


def _audit_manifests(audit_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = audit_root.expanduser().resolve()
    if not root.exists():
        return []
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.rglob("manifest.json")):
        if path.is_symlink() or not path.is_file():
            continue
        bundle = path.parent.resolve()
        try:
            if not bundle.is_relative_to(root):
                continue
            manifest = _read_audit_manifest(bundle)
        except (PlanError, OSError):
            continue
        result.append((bundle, manifest))
    return result


def resolve_audit_bundle(
    reference: str,
    audit_root: Path,
    *,
    allow_invalid: bool = False,
) -> Path:
    root = audit_root.expanduser().resolve()
    candidate = Path(reference).expanduser()
    if candidate.exists():
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.name == "manifest.json":
            resolved = resolved.parent
        if not resolved.is_relative_to(root):
            raise PlanError(f"audit bundle is outside configured audit root: {resolved}")
        try:
            _read_audit_manifest(resolved)
        except PlanError:
            if not allow_invalid or not _audit_manifest_path(resolved).is_file():
                raise
        return resolved
    matches = [
        bundle
        for bundle, manifest in _audit_manifests(root)
        if manifest.get("audit_id") == reference
    ]
    if allow_invalid and root.exists():
        for path in root.rglob("manifest.json"):
            if path.is_file() and not path.is_symlink() and path.parent.name == reference:
                matches.append(path.parent.resolve())
    matches = sorted(set(matches))
    if not matches:
        raise PlanError(f"audit bundle not found: {reference}")
    if len(matches) > 1:
        raise PlanError(f"audit_id is ambiguous: {reference}")
    return matches[0]


def _audit_row(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    retention = manifest.get("retention") if isinstance(manifest.get("retention"), dict) else {}
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    size_bytes = 0
    for path in bundle.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                size_bytes += path.stat().st_size
            except OSError:
                pass
    return {
        "audit_id": manifest.get("audit_id"),
        "bundle_version": manifest.get("bundle_version"),
        "skill_release": manifest.get("skill_release"),
        "planner_version": manifest.get("planner_version"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "closed_at": manifest.get("closed_at"),
        "delete_after": retention.get("delete_after"),
        "keep": retention.get("keep", False),
        "risk": manifest.get("task", {}).get("risk") if isinstance(manifest.get("task"), dict) else None,
        "repository": manifest.get("repository", {}).get("name") if isinstance(manifest.get("repository"), dict) else None,
        "task_name": manifest.get("task", {}).get("name") if isinstance(manifest.get("task"), dict) else None,
        "attention_required": summary.get("attention_required", False),
        "plan_count": summary.get("plan_count"),
        "accepted_attempt_count": summary.get("accepted_attempt_count"),
        "size_bytes": size_bytes,
        "path": str(bundle),
    }


def list_audit_bundles(
    audit_root: Path,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if status not in {None, "open", "closed", "invalid"}:
        raise PlanError("audit status filter must be open, closed, or invalid")
    root = audit_root.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    valid_manifest_paths: set[Path] = set()
    for bundle, manifest in _audit_manifests(root):
        valid_manifest_paths.add(_audit_manifest_path(bundle).resolve())
        if status is None or manifest.get("status") == status:
            rows.append(_audit_row(bundle, manifest))
    if root.exists() and status in {None, "invalid"}:
        for path in sorted(root.rglob("manifest.json")):
            resolved = path.resolve()
            if resolved in valid_manifest_paths or path.is_symlink() or not path.is_file():
                continue
            try:
                size_bytes = sum(
                    child.stat().st_size
                    for child in path.parent.rglob("*")
                    if child.is_file() and not child.is_symlink()
                )
            except OSError:
                size_bytes = 0
            rows.append(
                {
                    "audit_id": path.parent.name,
                    "bundle_version": None,
                    "skill_release": None,
                    "planner_version": None,
                    "status": "invalid",
                    "created_at": None,
                    "closed_at": None,
                    "delete_after": None,
                    "keep": False,
                    "risk": None,
                    "repository": path.parent.parent.name,
                    "task_name": "<unreadable manifest>",
                    "attention_required": True,
                    "plan_count": None,
                    "accepted_attempt_count": None,
                    "size_bytes": size_bytes,
                    "path": str(path.parent),
                }
            )
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise PlanError("audit list limit must be a positive integer")
        rows = rows[:limit]
    return rows


def show_audit_bundle(bundle: Path) -> dict[str, Any]:
    manifest = _read_audit_manifest(bundle)
    return {
        **manifest,
        "audit_dir": str(bundle),
        "size_bytes": _audit_row(bundle, manifest)["size_bytes"],
    }


def delete_audit_bundle(
    bundle: Path,
    *,
    confirmed: bool,
    force: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise PlanError("audit-delete requires --yes")
    try:
        manifest = _read_audit_manifest(bundle)
    except PlanError:
        if not force:
            raise
        manifest = {
            "audit_id": bundle.name,
            "status": "invalid",
            "summary": {},
            "verification": {},
        }
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    verification = (
        manifest.get("verification")
        if isinstance(manifest.get("verification"), dict)
        else {}
    )
    if not force:
        if manifest.get("status") != "closed":
            raise PlanError("refusing to delete an open audit bundle without --force")
        if verification.get("status") != "passed":
            raise PlanError("refusing to delete an unverified audit bundle without --force")
        current_report = inspect_audit_bundle(bundle, compare_manifest=True)
        if current_report["status"] != "passed":
            raise PlanError(
                "refusing to delete an audit bundle that no longer passes verification "
                "without --force"
            )
        if current_report["summary"].get("attention_required") or summary.get(
            "attention_required"
        ):
            raise PlanError("refusing to delete an audit bundle with anomalies without --force")
    audit_id = manifest.get("audit_id")
    path = str(bundle)
    shutil.rmtree(bundle)
    parent = bundle.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return {
        "report_type": "MULTI_AGENT_AUDIT_DELETE",
        "status": "deleted",
        "audit_id": audit_id,
        "path": path,
    }


def prune_audit_bundles(
    audit_root: Path,
    *,
    apply: bool,
    include_attention: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _utc_now()
    selected: list[dict[str, Any]] = []
    skipped_attention: list[str] = []
    skipped_unverified: list[str] = []
    for bundle, manifest in _audit_manifests(audit_root):
        if manifest.get("status") != "closed":
            continue
        verification = manifest.get("verification")
        if not isinstance(verification, dict) or verification.get("status") != "passed":
            continue
        retention = manifest.get("retention")
        if not isinstance(retention, dict) or retention.get("keep"):
            continue
        delete_after = retention.get("delete_after")
        if delete_after is None:
            continue
        try:
            expired = _parse_iso_z(delete_after, "audit.retention.delete_after") <= current
        except PlanError:
            continue
        if not expired:
            continue
        current_report = inspect_audit_bundle(bundle, compare_manifest=True)
        if current_report["status"] != "passed":
            skipped_unverified.append(str(manifest.get("audit_id")))
            continue
        summary = current_report["summary"]
        if summary.get("attention_required") and not include_attention:
            skipped_attention.append(str(manifest.get("audit_id")))
            continue
        row = _audit_row(bundle, manifest)
        selected.append(row)
        if apply:
            shutil.rmtree(bundle)
            try:
                if bundle.parent.is_dir() and not any(bundle.parent.iterdir()):
                    bundle.parent.rmdir()
            except OSError:
                pass
    return {
        "report_type": "MULTI_AGENT_AUDIT_PRUNE",
        "status": "applied" if apply else "dry-run",
        "audit_root": str(audit_root.expanduser().resolve()),
        "selected_count": len(selected),
        "selected": selected,
        "skipped_attention_audit_ids": skipped_attention,
        "skipped_unverified_audit_ids": skipped_unverified,
    }


def render_audit_bundle_report(report: dict[str, Any]) -> str:
    lines = [
        "MULTI_AGENT_AUDIT_BUNDLE",
        f"status: {report['status']}",
        f"audit_id: {report.get('audit_id')}",
        f"audit_dir: {report.get('audit_dir')}",
        f"bundle_status: {report.get('bundle_status')}",
        f"artifact_count: {report.get('artifact_count')}",
        f"size_bytes: {report.get('size_bytes')}",
        "summary: " + _json_inline(report.get("summary")),
    ]
    if report.get("errors"):
        lines.append("errors:")
        lines.extend(f"  - {item}" for item in report["errors"])
    else:
        lines.append("errors: []")
    if report.get("warnings"):
        lines.append("warnings:")
        lines.extend(f"  - {item}" for item in report["warnings"])
    else:
        lines.append("warnings: []")
    return "\n".join(lines) + "\n"


def render_audit_list(rows: list[dict[str, Any]], audit_root: Path) -> str:
    lines = [
        f"MULTI_AGENT_AUDITS root={audit_root.expanduser().resolve()}",
        "audit_id | bundle | skill | status | attention | delete_after | repository | task | size_bytes",
    ]
    if not rows:
        lines.append("(none)")
        return "\n".join(lines) + "\n"
    for row in rows:
        lines.append(
            " | ".join(
                [
                    str(row.get("audit_id")),
                    str(row.get("bundle_version") or "?"),
                    str(row.get("skill_release") or "?"),
                    str(row.get("status")),
                    "yes" if row.get("attention_required") else "no",
                    str(row.get("delete_after") or "keep"),
                    str(row.get("repository") or "unknown"),
                    str(row.get("task_name") or "unknown"),
                    str(row.get("size_bytes") or 0),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def render_audit_show(value: dict[str, Any]) -> str:
    retention = value.get("retention") if isinstance(value.get("retention"), dict) else {}
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    lines = [
        "MULTI_AGENT_AUDIT",
        f"audit_id: {value.get('audit_id')}",
        f"bundle_version: {value.get('bundle_version')}",
        f"skill_release: {value.get('skill_release')}",
        f"planner_version: {value.get('planner_version')}",
        f"status: {value.get('status')}",
        f"task: {value.get('task', {}).get('name') if isinstance(value.get('task'), dict) else None}",
        f"repository: {value.get('repository', {}).get('root') if isinstance(value.get('repository'), dict) else None}",
        f"created_at: {value.get('created_at')}",
        f"closed_at: {value.get('closed_at')}",
        f"delete_after: {retention.get('delete_after') or 'keep'}",
        f"size_bytes: {value.get('size_bytes')}",
        f"summary: {_json_inline(summary)}",
        f"audit_dir: {value.get('audit_dir')}",
    ]
    return "\n".join(lines) + "\n"


def render_audit_prune(report: dict[str, Any]) -> str:
    lines = [
        "MULTI_AGENT_AUDIT_PRUNE",
        f"status: {report['status']}",
        f"audit_root: {report['audit_root']}",
        f"selected_count: {report['selected_count']}",
    ]
    for row in report["selected"]:
        lines.append(f"  - {row['audit_id']}: {row['path']}")
    if report["skipped_attention_audit_ids"]:
        lines.append(
            "skipped_attention: "
            + ", ".join(report["skipped_attention_audit_ids"])
        )
    else:
        lines.append("skipped_attention: []")
    if report["skipped_unverified_audit_ids"]:
        lines.append(
            "skipped_unverified: "
            + ", ".join(report["skipped_unverified_audit_ids"])
        )
    else:
        lines.append("skipped_unverified: []")
    return "\n".join(lines) + "\n"

def build_doctor_report(
    roles: RoleRegistry,
    codex_config: dict[str, Any],
    audit_root: Path | None = None,
) -> dict[str, Any]:
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
    audit_info: dict[str, Any] | None = None
    if audit_root is not None:
        root = audit_root.expanduser().resolve()
        exists = root.exists()
        writable = root.is_dir() and os.access(root, os.W_OK) if exists else False
        audit_info = {
            "path": str(root),
            "exists": exists,
            "writable": writable,
        }
        if exists and not root.is_dir():
            errors.append("persistent audit root exists but is not a directory")
        elif exists and not writable:
            errors.append("persistent audit root is not writable")
        elif not exists:
            warnings.append("persistent audit root does not exist yet; audit-init will create it")

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
        "audit_root": audit_info,
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
    digest = validate_digest_snapshot_for_render(digest)
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
        "audit_root: " + _json_inline(report.get("audit_root")),
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
    parser.add_argument(
        "--audit-root",
        type=Path,
        default=default_audit_root(),
        help=(
            "Persistent audit root. Defaults to "
            "$MULTI_AGENT_AUDIT_ROOT or $CODEX_HOME/audits/multi-agent."
        ),
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
    render_digest_parser = subparsers.add_parser(
        "render-digest",
        help=(
            "Render an existing validated Digest JSON snapshot without "
            "re-reading current Agent TOMLs or config.toml."
        ),
    )
    render_digest_parser.add_argument("input", type=Path)
    render_digest_parser.add_argument("--output", type=Path)
    guard_parser = subparsers.add_parser(
        "guard-dispatch",
        help="Validate a normalized spawn_agent/followup_task payload before dispatch.",
    )
    guard_parser.add_argument("plan", type=Path)
    guard_parser.add_argument("task_id")
    guard_parser.add_argument("dispatch", type=Path)
    guard_parser.add_argument(
        "--audit",
        required=True,
        help=(
            "Persistent audit ID or bundle path. The plan and normalized "
            "dispatch files must belong to the same allocated open stage."
        ),
    )
    guard_parser.add_argument("--output", type=Path)
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check fixed-profile Agent TOMLs and Codex runtime configuration."
    )
    doctor_parser.add_argument("--output", type=Path)
    doctor_parser.add_argument("--json", action="store_true")

    audit_init_parser = subparsers.add_parser(
        "audit-init",
        help="Create a persistent versioned audit bundle and print its directory.",
    )
    audit_init_parser.add_argument("--task-name", required=True)
    audit_init_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    audit_init_parser.add_argument("--risk", choices=("normal", "high"), default="normal")
    audit_init_parser.add_argument("--retention-days", type=int)
    audit_init_parser.add_argument("--keep", action="store_true")
    audit_init_parser.add_argument("--json", action="store_true")
    audit_init_parser.add_argument("--output", type=Path)

    audit_import_parser = subparsers.add_parser(
        "audit-import",
        help="Import an existing temporary audit directory into persistent storage.",
    )
    audit_import_parser.add_argument("source", type=Path)
    audit_import_parser.add_argument("--task-name", required=True)
    audit_import_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    audit_import_parser.add_argument("--risk", choices=("normal", "high"), default="normal")
    audit_import_parser.add_argument("--retention-days", type=int)
    audit_import_parser.add_argument("--keep", action="store_true")
    audit_import_parser.add_argument("--json", action="store_true")
    audit_import_parser.add_argument("--output", type=Path)

    audit_stage_parser = subparsers.add_parser(
        "audit-stage",
        help="Allocate deterministic artifact paths for one WorkPlan stage.",
    )
    audit_stage_parser.add_argument("audit")
    audit_stage_parser.add_argument("--name", required=True)
    audit_stage_parser.add_argument("--output", type=Path)

    audit_finalize_parser = subparsers.add_parser(
        "audit-finalize",
        help="Render missing human-readable artifacts, verify, checksum, and close a bundle.",
    )
    audit_finalize_parser.add_argument("audit")
    audit_finalize_parser.add_argument("--retention-days", type=int)
    audit_finalize_parser.add_argument("--keep", action="store_true")
    audit_finalize_parser.add_argument("--json", action="store_true")
    audit_finalize_parser.add_argument("--output", type=Path)

    audit_verify_parser = subparsers.add_parser(
        "audit-verify",
        help="Verify a closed bundle against its versioned manifest and checksums.",
    )
    audit_verify_parser.add_argument("audit")
    audit_verify_parser.add_argument("--json", action="store_true")
    audit_verify_parser.add_argument("--output", type=Path)

    audit_list_parser = subparsers.add_parser(
        "audit-list", help="List discoverable persistent audit bundles."
    )
    audit_list_parser.add_argument("--status", choices=("open", "closed", "invalid"))
    audit_list_parser.add_argument("--limit", type=int)
    audit_list_parser.add_argument("--json", action="store_true")
    audit_list_parser.add_argument("--output", type=Path)

    audit_show_parser = subparsers.add_parser(
        "audit-show", help="Show one audit bundle by path or audit_id."
    )
    audit_show_parser.add_argument("audit")
    audit_show_parser.add_argument("--json", action="store_true")
    audit_show_parser.add_argument("--output", type=Path)

    audit_delete_parser = subparsers.add_parser(
        "audit-delete", help="Safely delete one audit bundle by path or audit_id."
    )
    audit_delete_parser.add_argument("audit")
    audit_delete_parser.add_argument("--yes", action="store_true")
    audit_delete_parser.add_argument("--force", action="store_true")
    audit_delete_parser.add_argument("--json", action="store_true")
    audit_delete_parser.add_argument("--output", type=Path)

    audit_prune_parser = subparsers.add_parser(
        "audit-prune",
        help="Dry-run or delete closed audit bundles whose retention has expired.",
    )
    audit_prune_parser.add_argument("--apply", action="store_true")
    audit_prune_parser.add_argument("--include-attention", action="store_true")
    audit_prune_parser.add_argument("--json", action="store_true")
    audit_prune_parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "render-digest":
            _write_text(render_execution_digest(_read_json(args.input)), args.output)
            return 0

        if args.command == "audit-init":
            bundle, manifest = create_audit_bundle(
                args.audit_root,
                args.repo_root,
                args.task_name,
                risk=args.risk,
                retention_days=args.retention_days,
                keep=args.keep,
            )
            descriptor = _audit_output_descriptor(bundle, manifest)
            if args.json:
                _write_json(descriptor, args.output)
            elif args.output is not None:
                _write_text(str(bundle) + "\n", args.output)
            else:
                print(bundle)
            return 0

        if args.command == "audit-import":
            bundle, report = import_audit_directory(
                args.audit_root,
                args.source,
                args.repo_root,
                args.task_name,
                risk=args.risk,
                retention_days=args.retention_days,
                keep=args.keep,
            )
            report["audit_dir"] = str(bundle)
            if args.json:
                _write_json(report, args.output)
            else:
                _write_text(render_audit_bundle_report(report), args.output)
            return 0 if report["status"] == "passed" else 2

        if args.command == "audit-stage":
            bundle = resolve_audit_bundle(args.audit, args.audit_root)
            _write_json(allocate_audit_stage(bundle, args.name), args.output)
            return 0

        if args.command == "audit-finalize":
            bundle = resolve_audit_bundle(args.audit, args.audit_root)
            report = finalize_audit_bundle(
                bundle,
                retention_days=args.retention_days,
                keep=args.keep,
            )
            if args.json:
                _write_json(report, args.output)
            else:
                _write_text(render_audit_bundle_report(report), args.output)
            return 0 if report["status"] == "passed" else 2

        if args.command == "audit-verify":
            bundle = resolve_audit_bundle(args.audit, args.audit_root)
            report = inspect_audit_bundle(bundle, compare_manifest=True)
            if args.json:
                _write_json(report, args.output)
            else:
                _write_text(render_audit_bundle_report(report), args.output)
            return 0 if report["status"] == "passed" else 2

        if args.command == "audit-list":
            rows = list_audit_bundles(
                args.audit_root,
                status=args.status,
                limit=args.limit,
            )
            if args.json:
                _write_json(
                    {
                        "report_type": "MULTI_AGENT_AUDIT_LIST",
                        "audit_root": str(args.audit_root.expanduser().resolve()),
                        "count": len(rows),
                        "audits": rows,
                    },
                    args.output,
                )
            else:
                _write_text(render_audit_list(rows, args.audit_root), args.output)
            return 0

        if args.command == "audit-show":
            bundle = resolve_audit_bundle(args.audit, args.audit_root)
            value = show_audit_bundle(bundle)
            if args.json:
                _write_json(value, args.output)
            else:
                _write_text(render_audit_show(value), args.output)
            return 0

        if args.command == "audit-delete":
            bundle = resolve_audit_bundle(
                args.audit, args.audit_root, allow_invalid=args.force
            )
            report = delete_audit_bundle(
                bundle,
                confirmed=args.yes,
                force=args.force,
            )
            if args.json:
                _write_json(report, args.output)
            else:
                _write_text(
                    f"deleted: {report['audit_id']}\npath: {report['path']}\n",
                    args.output,
                )
            return 0

        if args.command == "audit-prune":
            report = prune_audit_bundles(
                args.audit_root,
                apply=args.apply,
                include_attention=args.include_attention,
            )
            if args.json:
                _write_json(report, args.output)
            else:
                _write_text(render_audit_prune(report), args.output)
            return 0

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
            bundle = resolve_audit_bundle(args.audit, args.audit_root)
            audit_membership = guard_audit_membership(
                bundle, args.plan, args.dispatch
            )
            result = guard_dispatch(
                _read_json(args.plan),
                args.task_id,
                _read_json(args.dispatch),
                roles,
                codex_config,
            )
            result.update(audit_membership)
            _write_json(result, args.output)
        else:
            report = build_doctor_report(roles, codex_config, args.audit_root)
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
