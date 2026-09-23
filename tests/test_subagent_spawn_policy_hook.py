"""通过 stdin、退出码和输出验证真实 Hook 进程的门禁合同。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "bin" / "subagent-spawn-policy-hook"


class SpawnPolicyHookTests(unittest.TestCase):
    def event(self, tool_name="spawn_agent"):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": {
                "task_name": "review_hook",
                "agent_type": "critical_reviewer",
                "fork_turns": "none",
            },
        }

    def run_hook(self, event, *, raw=False):
        return subprocess.run(
            [str(HOOK)],
            input=event if raw else json.dumps(event),
            text=True,
            capture_output=True,
            cwd=ROOT.parent,
            env={**os.environ, "MULTI_AGENT_ORCHESTRATION_PYTHON": sys.executable},
            timeout=10,
        )

    def assert_allowed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def assert_denied(self, result, field):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("SUBAGENT_SPAWN_POLICY_DENY", result.stderr)
        self.assertIn(field, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_valid_spawn(self):
        self.assert_allowed(self.run_hook(self.event()))

    def test_required_names(self):
        for field in ("task_name", "agent_type"):
            event = self.event()
            del event["tool_input"][field]
            with self.subTest(field=field, missing=True):
                self.assert_denied(self.run_hook(event), field)
            for value in (None, "", " \t\n", 0, False, [], {}):
                with self.subTest(field=field, value=value):
                    event["tool_input"][field] = value
                    self.assert_denied(self.run_hook(event), field)

    def test_fork_turns_must_be_explicit_none(self):
        event = self.event()
        del event["tool_input"]["fork_turns"]
        self.assert_denied(self.run_hook(event), "fork_turns")
        for value in ("all", "1", "None", " none ", None, 0, False, [], {}):
            with self.subTest(value=value):
                event["tool_input"]["fork_turns"] = value
                self.assert_denied(self.run_hook(event), "fork_turns")

    def test_nonempty_overrides_are_denied(self):
        for field in ("model", "reasoning_effort", "model_reasoning_effort"):
            for value in ("override", " ", 0, False, [], {}, ["override"]):
                with self.subTest(field=field, value=value):
                    event = self.event()
                    event["tool_input"][field] = value
                    self.assert_denied(self.run_hook(event), field)

    def test_empty_overrides_are_allowed(self):
        for value in (None, ""):
            with self.subTest(value=value):
                event = self.event()
                for field in ("model", "reasoning_effort", "model_reasoning_effort"):
                    event["tool_input"][field] = value
                self.assert_allowed(self.run_hook(event))

    def test_top_level_model_is_not_an_override(self):
        event = self.event()
        event["model"] = "parent-model"
        self.assert_allowed(self.run_hook(event))
        event["tool_input"]["model"] = "child-override"
        self.assert_denied(self.run_hook(event), "model")

    def test_alias_and_namespaced_tools(self):
        config = json.loads((ROOT / "examples" / "hooks.user.json").read_text())
        matcher = re.compile(config["hooks"]["PreToolUse"][0]["matcher"])
        for name in ("spawn_agent", "Agent", "agents.spawn_agent",
                     "functions.collaboration.spawn_agent", "mcp__agents__spawn_agent",
                     "namespace/spawn_agent", "namespace::spawn_agent"):
            with self.subTest(name=name):
                self.assertIsNotNone(matcher.search(name))
                event = self.event(name)
                self.assert_allowed(self.run_hook(event))
                event["tool_input"]["fork_turns"] = "all"
                self.assert_denied(self.run_hook(event), "fork_turns")

    def test_other_tools_are_ignored(self):
        config = json.loads((ROOT / "examples" / "hooks.user.json").read_text())
        matcher = re.compile(config["hooks"]["PreToolUse"][0]["matcher"])
        for name in ("Bash", "followup_task", "spawn_agent_status", "AgentStatus"):
            self.assertIsNone(matcher.search(name))
            for value in ({"fork_turns": "all", "model": "override"}, None, "raw"):
                with self.subTest(name=name, value=value):
                    event = self.event(name)
                    event["tool_input"] = value
                    self.assert_allowed(self.run_hook(event))

    def test_rejection_does_not_echo_override(self):
        event = self.event()
        event["tool_input"]["model"] = "private-override-value"
        result = self.run_hook(event)
        self.assert_denied(result, "model")
        self.assertNotIn("private-override-value", result.stderr)

    def test_tool_input_must_be_object(self):
        event = self.event()
        del event["tool_input"]
        self.assert_denied(self.run_hook(event), "tool_input")
        for value in (None, "{}", [], 1, False):
            with self.subTest(value=value):
                event["tool_input"] = value
                self.assert_denied(self.run_hook(event), "tool_input")

    def test_top_level_must_be_object(self):
        for value in (None, [], "event", 1, False):
            with self.subTest(value=value):
                self.assert_denied(self.run_hook(value), "JSON object")

    def test_invalid_json_fails_closed(self):
        for value in ("", "{", "{}{}", '{"secret": private-value}', "NaN", "Infinity"):
            with self.subTest(value=value):
                result = self.run_hook(value, raw=True)
                self.assert_denied(result, "JSON")
                self.assertNotIn("private-value", result.stderr)

    def test_missing_event_name_cannot_bypass_policy(self):
        event = self.event()
        del event["hook_event_name"]
        event["tool_input"]["fork_turns"] = "all"
        self.assert_denied(self.run_hook(event), "fork_turns")


if __name__ == "__main__":
    unittest.main()
