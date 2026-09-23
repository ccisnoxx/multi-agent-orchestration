#!/usr/bin/env python3
"""无状态的固定角色 Profile 子代理 PreToolUse 门禁。"""

import json
import sys
from typing import Any


def reject(reason: str) -> int:
    """以 Codex 的阻止退出码拒绝待执行调用。"""
    print(
        "SUBAGENT_SPAWN_POLICY_DENY: " + reason,
        file=sys.stderr,
    )
    return 2


def is_spawn_tool(tool_name: Any) -> bool:
    if not isinstance(tool_name, str):
        return False

    return tool_name == "Agent" or tool_name.endswith("spawn_agent")


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def invalid_constant(value: str) -> None:
    """拒绝 Python JSON 解码器默认接受的非 JSON 数值常量。"""
    raise ValueError("非标准 JSON 常量")


def main() -> int:
    try:
        payload = json.load(sys.stdin, parse_constant=invalid_constant)
    except (ValueError, OSError, RecursionError):
        # 不回显输入或异常内容，避免把工具参数中的敏感数据写入日志。
        return reject("Hook 输入无法读取或不是有效 JSON")

    if not isinstance(payload, dict):
        return reject("Hook 输入必须是 JSON object")

    tool_name = payload.get("tool_name")
    if not is_spawn_tool(tool_name):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return reject("spawn_agent 的 tool_input 必须是 JSON object")

    if not nonempty_string(tool_input.get("task_name")):
        return reject("spawn_agent 必须显式提供非空字符串 task_name")

    if not nonempty_string(tool_input.get("agent_type")):
        return reject("spawn_agent 必须显式提供非空字符串 agent_type")

    if tool_input.get("fork_turns") != "none":
        return reject(
            'spawn_agent 必须显式使用 fork_turns="none"；'
            "禁止省略、all 或其他继承模式"
        )

    # 只检查 tool_input 中的覆盖参数。
    # Hook 顶层的 model 是当前主代理模型，不属于 spawn override。
    forbidden_overrides = (
        "model",
        "reasoning_effort",
        "model_reasoning_effort",
    )

    for field in forbidden_overrides:
        if tool_input.get(field) not in (None, ""):
            return reject(
                "固定角色 Profile 禁止在 spawn_agent 中覆盖 " + field
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
