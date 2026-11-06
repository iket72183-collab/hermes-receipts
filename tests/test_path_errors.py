"""Invalid inputs and adapter failures must never strand a capture."""
import json
import unittest
from unittest.mock import patch

import test_receipts as fixtures

tools, checks, storage = fixtures.tools, fixtures.checks, fixtures.storage


class PathErrorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_nul_path_refused_before_open(self):
        f = self.fixture
        for root, path in ((str(f.root), "bad\x00path"), (str(f.root) + "\x00", "page.html")):
            with self.subTest(path=path), patch.object(checks.os, "open", side_effect=AssertionError("invalid path opened")):
                result = checks.check_file({"path": path, "contains": "Ready"}, root)
                self.assertEqual(result["state"], "unverified")
                self.assertEqual(result["evidence"], {"relative_path": path})

    def test_legacy_nul_path_seals_other_checks_and_releases_host(self):
        f = self.fixture
        raw = f.spec()
        raw["checks"].append({"id": "invalid", "kind": "file", "path": "bad\x00path", "contains": "Ready"})
        # Simulate an open attempt persisted by the old validator. New begin
        # rejects this input, but existing captures still need a sealed outcome.
        att = storage.new_attempt_id()
        with storage.transaction(f.db) as conn:
            storage.insert_attempt(conn, attempt_id=att, task_id="demo",
                                   host_task_id=f.identity["task_id"], host_session_id=f.identity["session_id"],
                                   digest=fixtures.schema.criteria_digest(raw), spec=raw, started_at=storage.utc_now())
        receipt = f.seal(att)
        self.assertEqual(receipt["state"], "unverified")
        self.assertEqual([item["state"] for item in receipt["checks"]], ["passed", "unverified"])
        self.assertEqual(receipt["checks"][1]["evidence"], {"relative_path": "bad\x00path"})
        self.assertEqual(f.seal(att), receipt)
        self.assertEqual(f.seal(f.begin())["state"], "verified")

    def test_os_path_value_error_is_unverified(self):
        f = self.fixture
        with patch.object(checks.os, "open", side_effect=ValueError("embedded null character")):
            receipt = f.seal(f.begin())
        self.assertEqual(receipt["state"], "unverified")
        self.assertEqual(receipt["checks"][0]["evidence"], {"relative_path": "page.html"})
        self.assertEqual(f.seal(f.begin())["state"], "verified")

    def test_adapter_exception_seals_without_discarding_evidence(self):
        f = self.fixture
        for kind, adapter in (("file", "check_file"), ("website", "check_website")):
            with self.subTest(kind=kind):
                raw = f.spec()
                failing = f.spec(kind)["checks"][0]
                failing["id"] = "broken"
                raw["checks"].append(failing)
                original = getattr(tools, adapter)

                def evaluate(check, *args, **kwargs):
                    if check["id"] == "broken":
                        raise RuntimeError("SENSITIVE_EXCEPTION_CANARY")
                    return original(check, *args, **kwargs)

                att = f.begin(raw)
                with patch.object(tools, adapter, side_effect=evaluate):
                    receipt = f.seal(att)
                self.assertEqual(receipt["state"], "unverified")
                self.assertEqual([item["state"] for item in receipt["checks"]], ["passed", "unverified"])
                self.assertEqual(receipt["checks"][1]["evidence"], {})
                self.assertNotIn("SENSITIVE_EXCEPTION_CANARY", json.dumps(receipt))
                self.assertEqual(f.seal(att), receipt)
                self.assertEqual(f.seal(f.begin())["state"], "verified")
