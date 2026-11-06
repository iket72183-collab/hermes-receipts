"""Reject ambiguous capture criteria and malformed paths before creating state."""

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT, plugin

schema = importlib.import_module(plugin.__name__ + ".schema")
storage = importlib.import_module(plugin.__name__ + ".storage")
tools, observe = plugin.tools, plugin.observe


class CaptureCriteriaTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "output" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "state" / "data.db"
        self.identity = {"task_id": "host-task", "session_id": "host-session"}
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.root / "hermes-home"
        modules = patch.dict(sys.modules, {"hermes_constants": constants})
        modules.start()
        self.addCleanup(modules.stop)
        tools.set_db_override(self.db)
        self.addCleanup(tools.set_db_override, None)
        observer_storage = patch.object(observe, "transaction", lambda: storage.transaction(self.db))
        observer_storage.start()
        self.addCleanup(observer_storage.stop)

    def spec(self):
        return {
            "schema_version": 1,
            "task_id": "duplicate-regression",
            "workspace_root": str(self.root),
            "checks": [{"id": "first", "kind": "test", "command": "printf Ready",
                        "workdir": str(self.root), "expected_exit": 0,
                        "contains": "Ready", "timeout": 5}],
        }

    def begin(self, raw):
        return json.loads(tools.receipts_begin({"spec": raw}, **self.identity))

    def capture_and_seal(self, raw):
        started = self.begin(raw)
        self.assertTrue(started.get("ok"), started)
        for check in raw["checks"]:
            args = {"command": check["command"], "workdir": check["workdir"]}
            observe.on_pre_tool_call(tool_name="terminal", args=args,
                                     tool_call_id=check["id"], **self.identity)
            observe.on_post_tool_call(tool_name="terminal", args=args,
                                      tool_call_id=check["id"], status="ok", duration_ms=10,
                                      result={"exit_code": 0, "output": "Ready"}, **self.identity)
        receipt = json.loads(tools.receipts_check({"attempt_id": started["attempt_id"]}, **self.identity))
        self.assertEqual(receipt["state"], "verified", receipt)
        return receipt

    def test_duplicate_command_workdir_rejected_even_with_different_expectations(self):
        for changed in ({}, {"expected_exit": 2}, {"contains": "Different"}, {"timeout": 10}):
            with self.subTest(changed=changed):
                raw = self.spec()
                raw["checks"].append({**raw["checks"][0], "id": "second", **changed})
                with self.assertRaisesRegex(schema.SpecError, "second duplicates test command/workdir from first"):
                    schema.normalize_spec(raw)
                # Validation happens before storage opens, so a rejected spec
                # cannot create an open attempt or claim the host context.
                with patch.object(tools, "transaction", side_effect=AssertionError("storage opened")):
                    result = self.begin(raw)
                self.assertFalse(result["ok"])
                self.assertIn("one test check per exact command/workdir pair", result["error"])
        self.assertFalse(self.db.exists())
        self.capture_and_seal(self.spec())

    def test_distinct_commands_capture_independent_evidence(self):
        raw = self.spec()
        raw["checks"].append({**raw["checks"][0], "id": "second", "command": "echo Ready"})
        receipt = self.capture_and_seal(raw)
        self.assertEqual([check["state"] for check in receipt["checks"]], ["passed", "passed"])

    def test_distinct_workdirs_capture_independent_evidence(self):
        raw = self.spec()
        workdir = self.root / "other"
        workdir.mkdir()
        raw["checks"].append({**raw["checks"][0], "id": "second", "workdir": str(workdir)})
        receipt = self.capture_and_seal(raw)
        self.assertEqual([check["state"] for check in receipt["checks"]], ["passed", "passed"])

    def test_nul_paths_rejected_before_storage_and_allow_next_attempt(self):
        for field in ("workspace_root", "workdir", "path"):
            with self.subTest(field=field):
                raw = self.spec()
                if field == "workspace_root":
                    raw[field] += "\x00suffix"
                elif field == "workdir":
                    raw["checks"][0][field] += "\x00suffix"
                else:
                    raw["checks"] = [{"id": "file", "kind": "file", "path": "page\x00.html", "contains": "Ready"}]
                with self.assertRaisesRegex(schema.SpecError, field + " must not contain NUL"):
                    schema.normalize_spec(raw)
                with patch.object(tools, "transaction", side_effect=AssertionError("storage opened")):
                    result = self.begin(raw)
                self.assertFalse(result["ok"])
                self.assertIn("NUL", result["error"])
        self.assertFalse(self.db.exists())
        self.capture_and_seal(self.spec())


if __name__ == "__main__":
    unittest.main()
