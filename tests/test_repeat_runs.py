"""Regression tests for repeated terminal runs, seal races, ordering, and ancestor paths."""

import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_receipts as fixtures

tools = fixtures.tools
observe = fixtures.observe
checks = fixtures.checks
storage = fixtures.storage
schema = fixtures.schema


class RepeatRunAndBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def _run_terminal(self, call_id, *, exit_code=0, output="Ready", status="ok", duration=10):
        f = self.fixture
        args = {"command": "printf Ready", "workdir": str(f.root)}
        observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id=call_id, **f.identity)
        observe.on_post_tool_call(
            tool_name="terminal",
            args=args,
            result=json.dumps({"exit_code": exit_code, "output": output}),
            tool_call_id=call_id,
            status=status,
            duration_ms=duration,
            **f.identity,
        )

    def test_pass_then_fail_in_same_attempt_seals_failed(self):
        f = self.fixture
        t0 = datetime(2026, 11, 7, 19, 0, 0, tzinfo=timezone.utc)
        t1 = (t0 + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t2 = (t0 + timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for same_second in (True, False):
            tag = "same" if same_second else "later"
            with self.subTest(same_second=same_second):
                if same_second:
                    att = f.begin(f.spec("test"))
                    self._run_terminal(f"{tag}-pass", exit_code=0, output="Ready")
                    self._run_terminal(f"{tag}-fail", exit_code=1, output="Broken", status="error")
                    receipt = f.seal(att)
                else:
                    t0_str = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
                    with patch.object(tools, "utc_now", return_value=t0_str), \
                         patch.object(storage, "utc_now", return_value=t0_str), \
                         patch.object(observe, "utc_now", return_value=t0_str):
                        att = f.begin(f.spec("test"))
                        self._run_terminal(f"{tag}-pass", exit_code=0, output="Ready")
                    with patch.object(storage, "utc_now", return_value=t1), \
                         patch.object(observe, "utc_now", return_value=t1):
                        self._run_terminal(f"{tag}-fail", exit_code=1, output="Broken", status="error")
                    with patch.object(tools, "utc_now", return_value=t2):
                        receipt = f.seal(att)
                self.assertEqual(receipt["state"], "failed", receipt)
                check = receipt["checks"][0]
                self.assertEqual(check["state"], "failed")
                self.assertIn("exit 1 != expected 0", check["reason"])
                self.assertEqual(check["evidence"]["tool_call_id"], f"{tag}-fail")
                self.assertEqual(check["evidence"]["exit_code"], 1)

    def test_fail_then_pass_in_same_attempt_preserves_failure(self):
        f = self.fixture
        att = f.begin(f.spec("test"))
        self._run_terminal("call-fail", exit_code=2, output="Broken", status="error")
        self._run_terminal("call-pass", exit_code=0, output="Ready")
        receipt = f.seal(att)
        self.assertEqual(receipt["state"], "failed", receipt)
        check = receipt["checks"][0]
        self.assertEqual(check["state"], "failed")
        self.assertIn("exit 2 != expected 0", check["reason"])
        self.assertEqual(check["evidence"]["tool_call_id"], "call-fail")
        self.assertEqual(check["evidence"]["exit_code"], 2)

    def test_repeated_passes_in_same_attempt_seals_verified_with_latest(self):
        f = self.fixture
        att = f.begin(f.spec("test"))
        self._run_terminal("call-pass-1", exit_code=0, output="Ready")
        self._run_terminal("call-pass-2", exit_code=0, output="Ready")
        receipt = f.seal(att)
        self.assertEqual(receipt["state"], "verified", receipt)
        check = receipt["checks"][0]
        self.assertEqual(check["state"], "passed")
        self.assertEqual(check["evidence"]["tool_call_id"], "call-pass-2")

    def test_pass_with_timeout_or_incomplete_runs_seals_unverified(self):
        f = self.fixture
        scenarios = (
            ("pass-then-timeout", [("call-1", 0, "Ready", "ok", 10), ("call-2", 124, "Ready", "ok", 10)], "call-2"),
            ("timeout-then-pass", [("call-1", 124, "Ready", "ok", 10), ("call-2", 0, "Ready", "ok", 10)], "call-1"),
            ("pass-then-blocked", [("call-1", 0, "Ready", "ok", 10), ("call-2", 0, "Ready", "blocked", 10)], "call-2"),
            ("pass-then-over-timeout", [("call-1", 0, "Ready", "ok", 10), ("call-2", 0, "Ready", "ok", 6000)], "call-2"),
        )
        for label, runs, expected_call in scenarios:
            with self.subTest(label=label):
                att = f.begin(f.spec("test", timeout=5))
                for call_id, code, out, st, dur in runs:
                    self._run_terminal(f"{label}-{call_id}", exit_code=code, output=out, status=st, duration=dur)
                receipt = f.seal(att)
                self.assertEqual(receipt["state"], "unverified", receipt)
                check = receipt["checks"][0]
                self.assertEqual(check["state"], "unverified")
                self.assertEqual(check["evidence"]["tool_call_id"], f"{label}-{expected_call}")

    def test_duplicate_hook_delivery_keeps_single_observation_and_verifies(self):
        f = self.fixture
        att = f.begin(f.spec("test"))
        self._run_terminal("dup-call", exit_code=0, output="Ready")
        self._run_terminal("dup-call", exit_code=1, output="Ignored duplicate", status="error")
        with storage.transaction(f.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM pre_events WHERE attempt_id = ?", (att,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM observations WHERE attempt_id = ?", (att,)).fetchone()[0],
                1,
            )
        receipt = f.seal(att)
        self.assertEqual(receipt["state"], "verified", receipt)
        self.assertEqual(receipt["checks"][0]["evidence"]["tool_call_id"], "dup-call")

    def test_in_flight_run_racing_with_sealing_is_never_missed(self):
        f = self.fixture
        args = {"command": "printf Ready", "workdir": str(f.root)}

        # 1. Single in-flight pre_event when sealing -> unverified, late post ignored
        att1 = f.begin(f.spec("test"))
        observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id="in-flight-1", **f.identity)
        receipt1 = f.seal(att1)
        self.assertEqual(receipt1["state"], "unverified", receipt1)
        self.assertEqual(receipt1["checks"][0]["reason"], "terminal execution in progress or incomplete")
        self.assertEqual(receipt1["checks"][0]["evidence"]["tool_call_id"], "in-flight-1")
        observe.on_post_tool_call(
            tool_name="terminal",
            args=args,
            result=json.dumps({"exit_code": 0, "output": "Ready"}),
            tool_call_id="in-flight-1",
            status="ok",
            duration_ms=5,
            **f.identity,
        )
        self.assertEqual(f.seal(att1), receipt1)

        # 2. Passing run followed by in-flight pre_event racing with sealing -> unverified
        att2 = f.begin(f.spec("test"))
        self._run_terminal("done-pass", exit_code=0, output="Ready")
        observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id="in-flight-2", **f.identity)
        receipt2 = f.seal(att2)
        self.assertEqual(receipt2["state"], "unverified", receipt2)
        self.assertEqual(receipt2["checks"][0]["reason"], "terminal execution in progress or incomplete")
        self.assertEqual(receipt2["checks"][0]["evidence"]["tool_call_id"], "in-flight-2")

        # 3. Failing run followed by in-flight pre_event racing with sealing -> failed wins
        att3 = f.begin(f.spec("test"))
        self._run_terminal("done-fail", exit_code=1, output="Broken", status="error")
        observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id="in-flight-3", **f.identity)
        receipt3 = f.seal(att3)
        self.assertEqual(receipt3["state"], "failed", receipt3)
        self.assertEqual(receipt3["checks"][0]["evidence"]["tool_call_id"], "done-fail")

        # 4. Run completing during receipts_check file evaluation across a 1s clock tick
        raw = f.spec("file")
        raw["checks"].append({
            "id": "test",
            "kind": "test",
            "command": "printf Ready",
            "workdir": str(f.root),
            "expected_exit": 0,
            "contains": "Ready",
            "timeout": 5,
        })
        t_start = "2026-11-07T19:10:00Z"
        t_obs_1 = "2026-11-07T19:10:00Z"
        t_obs_2 = "2026-11-07T19:10:01Z"
        with patch.object(tools, "utc_now", return_value=t_start), \
             patch.object(storage, "utc_now", return_value=t_obs_1), \
             patch.object(observe, "utc_now", return_value=t_obs_1):
            att4 = f.begin(raw)
            self._run_terminal("early-pass", exit_code=0, output="Ready")

        real_check_file = tools.check_file

        def racing_file_check(check, workspace_root):
            res = real_check_file(check, workspace_root)
            with patch.object(storage, "utc_now", return_value=t_obs_2), \
                 patch.object(observe, "utc_now", return_value=t_obs_2):
                self._run_terminal("late-fail", exit_code=1, output="Broken", status="error")
            return res

        with patch.object(tools, "check_file", side_effect=racing_file_check), \
             patch.object(tools, "utc_now", return_value=t_obs_2):
            receipt4 = f.seal(att4)
        self.assertEqual(receipt4["state"], "failed", receipt4)
        self.assertEqual(receipt4["checks"][1]["state"], "failed")
        self.assertEqual(receipt4["checks"][1]["evidence"]["tool_call_id"], "late-fail")

    def test_same_second_attempts_preserve_insertion_order(self):
        f = self.fixture
        stamp = "2026-11-07T19:20:00Z"
        (f.root / "page.html").write_text("Wrong")
        with patch.object(tools, "utc_now", return_value=stamp), \
             patch.object(tools, "new_attempt_id", side_effect=["att_" + "f" * 24, "att_" + "0" * 24]):
            first = f.begin()
            r1 = f.seal(first)
            self.assertEqual(r1["state"], "failed")
            (f.root / "page.html").write_text("Ready")
            second = f.begin()
            r2 = f.seal(second)
            self.assertEqual(r2["state"], "verified")
        listed = json.loads(tools.receipts_get({"task_id": "demo"}))["attempts"]
        self.assertEqual([item["attempt_id"] for item in listed], [first, second])
        self.assertEqual([item["state"] for item in listed], ["failed", "verified"])

    def test_ancestor_workspace_refuses_non_denylisted_file_under_hermes_home(self):
        f = self.fixture
        f.home.mkdir(exist_ok=True)
        (f.home / "config.yaml").write_text("api_key: SYNTHETIC_HOME_SECRET\n")
        rel_target = f"{f.home.name}/config.yaml"
        raw = f.spec("file", path=rel_target, contains="SYNTHETIC_HOME_SECRET")
        att = f.begin(raw)
        with patch.object(checks.os, "open", side_effect=AssertionError("Hermes home target opened")):
            receipt = f.seal(att)
        self.assertEqual(receipt["state"], "unverified", receipt)
        check = receipt["checks"][0]
        self.assertEqual(check["state"], "unverified")
        self.assertIn("Hermes home", check["reason"])
        self.assertEqual(check["evidence"], {"relative_path": rel_target})


if __name__ == "__main__":
    unittest.main()
