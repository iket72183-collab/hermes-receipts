"""Capture ownership regressions using genuinely independent Python processes."""

import importlib
import json
import multiprocessing
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT, plugin

tools, observe = plugin.tools, plugin.observe
storage = importlib.import_module(plugin.__name__ + ".storage")


def _fork_epoch(pipe):
    pipe.send(storage.current_epoch())
    pipe.close()


class ProcessCaptureTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "output" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "state" / "data.db"
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.root / "hermes-home"
        modules = patch.dict(sys.modules, {"hermes_constants": constants})
        modules.start()
        self.addCleanup(modules.stop)
        tools.set_db_override(self.db)
        self.addCleanup(tools.set_db_override, None)
        hooks = patch.object(observe, "transaction", lambda: storage.transaction(self.db))
        hooks.start()
        self.addCleanup(hooks.stop)
        self.identity = {"task_id": "host-task", "session_id": "host-session"}
        self.args = {"command": "printf Ready", "workdir": str(self.root)}
        self.spec = {
            "schema_version": 1,
            "task_id": "process-test",
            "workspace_root": str(self.root),
            "checks": [{"id": "terminal", "kind": "test", **self.args,
                        "expected_exit": 0, "contains": "Ready", "timeout": 5}],
        }

    def begin(self):
        result = json.loads(tools.receipts_begin({"spec": self.spec}, **self.identity))
        self.assertTrue(result.get("ok"), result)
        return result["attempt_id"]

    def pre(self, call):
        observe.on_pre_tool_call(tool_name="terminal", args=self.args,
                                 tool_call_id=call, **self.identity)

    def post(self, call):
        observe.on_post_tool_call(tool_name="terminal", args=self.args,
                                  tool_call_id=call, result={"exit_code": 0, "output": "Ready"},
                                  status="ok", duration_ms=1, **self.identity)

    def seal(self, attempt):
        return json.loads(tools.receipts_check({"attempt_id": attempt}, **self.identity))

    def second_process(self, action, call="second-process"):
        code = '''
import importlib, json, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from support import plugin
storage = importlib.import_module(plugin.__name__ + ".storage")
db = Path(sys.argv[2])
spec = json.loads(sys.argv[3])
action, call = sys.argv[4:6]
plugin.tools.set_db_override(db)
plugin.observe.transaction = lambda: storage.transaction(db)
constants = types.ModuleType("hermes_constants")
constants.get_hermes_home = lambda: Path(spec["workspace_root"]) / "hermes-home"
sys.modules["hermes_constants"] = constants
identity = {"task_id": "host-task", "session_id": "host-session"}
args = {key: spec["checks"][0][key] for key in ("command", "workdir")}
if action == "lookup":
    result = json.loads(plugin.tools.receipts_get({"task_id": spec["task_id"]}))
elif action == "abandon":
    result = json.loads(plugin.tools.receipts_begin({"spec": spec}, **identity))
    plugin.observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id=call, **identity)
else:
    plugin.observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id="foreign-pre", **identity)
    plugin.observe.on_post_tool_call(tool_name="terminal", args=args, tool_call_id=call,
        result={"exit_code": 0, "output": "Ready"}, status="ok", duration_ms=1, **identity)
    result = {"ok": True}
print(json.dumps({"epoch": storage.current_epoch(), "result": result}))
'''
        completed = subprocess.run(
            [sys.executable, "-c", code, str(ROOT / "tests"), str(self.db),
             json.dumps(self.spec), action, call],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertNotEqual(result["epoch"], storage.current_epoch())
        return result["result"]

    def test_second_process_lookup_preserves_active_capture_at_each_boundary(self):
        for stage in ("before-pre", "between-pre-post", "after-post"):
            with self.subTest(stage=stage):
                attempt = self.begin()
                if stage == "before-pre":
                    self.assertTrue(self.second_process("lookup")["ok"])
                self.pre(stage)
                if stage == "between-pre-post":
                    self.assertTrue(self.second_process("lookup")["ok"])
                self.post(stage)
                if stage == "after-post":
                    self.assertTrue(self.second_process("lookup")["ok"])
                receipt = self.seal(attempt)
                self.assertEqual(receipt["state"], "verified", receipt)
                self.assertEqual(receipt["checks"][0]["evidence"]["tool_call_id"], stage)

    def test_second_process_cannot_capture_or_complete_owner_events(self):
        attempt = self.begin()
        self.pre("owner-call")
        self.second_process("hooks", "owner-call")
        with storage.transaction(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pre_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
        self.post("owner-call")
        self.assertEqual(self.seal(attempt)["state"], "verified")

    def test_exited_process_capture_cannot_be_completed_after_restart(self):
        abandoned = self.second_process("abandon", "abandoned-call")
        self.assertTrue(abandoned["ok"], abandoned)
        self.post("abandoned-call")
        self.pre("new-process-call")
        self.post("new-process-call")
        receipt = self.seal(abandoned["attempt_id"])
        self.assertEqual(receipt["state"], "unverified")
        fresh = self.begin()
        self.pre("fresh-call")
        self.post("fresh-call")
        self.assertEqual(self.seal(fresh)["state"], "verified")

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires POSIX fork")
    def test_fork_does_not_inherit_capture_epoch(self):
        parent_epoch = storage.current_epoch()
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=_fork_epoch, args=(sender,))
        child.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(10), "forked process did not return its epoch")
            self.assertNotEqual(receiver.recv(), parent_epoch)
        finally:
            receiver.close()
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        self.assertEqual(child.exitcode, 0)
