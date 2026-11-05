import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from support import ROOT, plugin

tools, observe = plugin.tools, plugin.observe
import importlib
schema = importlib.import_module(plugin.__name__ + ".schema")
storage = importlib.import_module(plugin.__name__ + ".storage")
checks = importlib.import_module(plugin.__name__ + ".checks")
exports = importlib.import_module(plugin.__name__ + ".export")


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "output" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.db = self.root / "state" / "data.db"
        tools.set_db_override(self.db)
        self.mock = patch.object(observe, "transaction", lambda: storage.transaction(self.db))
        self.mock.start()
        self.identity = {"task_id": "host-task", "session_id": "host-session"}
        (self.root / "page.html").write_text("Ready")

    def tearDown(self):
        self.mock.stop()
        tools.set_db_override(None)
        self.temp.cleanup()

    def spec(self, kind="file", **fields):
        check = {"id": "check", "kind": kind}
        if kind == "file": check.update(path="page.html", contains="Ready")
        if kind == "test": check.update(command="printf Ready", workdir=str(self.root), expected_exit=0, contains="Ready", timeout=5)
        if kind == "website": check.update(url="http://127.0.0.1:1/", expected_status=200, contains="Ready")
        check.update(fields)
        return {"schema_version": 1, "task_id": "demo", "workspace_root": str(self.root), "checks": [check]}

    def begin(self, spec=None, identity=True):
        result = json.loads(tools.receipts_begin({"spec": spec or self.spec()}, **(self.identity if identity else {})))
        self.assertTrue(result.get("ok"), result)
        return result["attempt_id"]

    def seal(self, attempt, **identity):
        return json.loads(tools.receipts_check({"attempt_id": attempt}, **(identity or self.identity)))

    def observe(self, *, result=None, call="call-1", status="ok", duration=10, **identity):
        ids = identity or self.identity
        args = {"command": "printf Ready", "workdir": str(self.root)}
        observe.on_pre_tool_call(tool_name="terminal", args=args, tool_call_id=call, **ids)
        observe.on_post_tool_call(tool_name="terminal", args=args, result=json.dumps(result or {"exit_code": 0, "output": "Ready"}), tool_call_id=call, status=status, duration_ms=duration, **ids)

    def test_empty_checks_rejected(self):
        raw = self.spec(); raw["checks"] = []
        with self.assertRaises(schema.SpecError): schema.normalize_spec(raw)

    def test_boolean_schema_version_rejected(self):
        raw=self.spec();raw['schema_version']=True
        with self.assertRaises(schema.SpecError):schema.normalize_spec(raw)

    def test_unknown_fields_and_model_evidence_rejected(self):
        raw = self.spec(); raw["evidence"] = {"exit_code": 0}
        with self.assertRaises(schema.SpecError): schema.normalize_spec(raw)
        att = self.begin()
        self.assertFalse(json.loads(tools.receipts_check({"attempt_id": att, "exit_code": 0}))['ok'])

    def test_unsafe_ids_rejected(self):
        for value in ("../x", "a/b", "x..y", "", "x' OR 1=1"):
            self.assertFalse(schema.valid_id(value))

    def test_duplicate_check_rejected(self):
        raw = self.spec(); raw["checks"] *= 2
        with self.assertRaises(schema.SpecError): schema.normalize_spec(raw)

    def test_successful_file(self):
        receipt = self.seal(self.begin())
        self.assertEqual(receipt['state'], 'verified')
        self.assertEqual(len(receipt['checks'][0]['evidence']['sha256']), 64)

    def test_missing_file_failed(self):
        self.assertEqual(checks.check_file(self.spec(path="absent")["checks"][0], str(self.root))["state"], "failed")

    def test_wrong_file_failed(self):
        (self.root / "page.html").write_text("Wrong")
        self.assertEqual(self.seal(self.begin())["state"], "failed")

    def test_json_format_and_keys(self):
        raw = {"kind": "file", "path": "page.html", "json_keys": ["ok"]}
        self.assertEqual(checks.check_file(raw, str(self.root))["state"], "failed")
        (self.root / "page.html").write_text('{"ok":true}')
        self.assertEqual(checks.check_file(raw, str(self.root))["state"], "passed")
        raw['json_keys'] = ['missing']
        self.assertEqual(checks.check_file(raw, str(self.root))["state"], "failed")

    def test_json_parser_limits_seal_mixed_checks_and_allow_new_attempt(self):
        raw = self.spec()
        raw['checks'].append({'id':'json', 'kind':'file', 'path':'nested.json', 'json_keys':['ok']})
        payloads = ('{"ok":' + '[' * 2000 + '0' + ']' * 2000 + '}',
                    '{"ok":' + '1' * 5000 + '}')
        for payload in payloads:
            with self.subTest(payload_size=len(payload)):
                (self.root / 'nested.json').write_text(payload)
                attempt = self.begin(raw)
                receipt = self.seal(attempt)
                self.assertEqual(receipt['state'], 'unverified')
                self.assertEqual([c['state'] for c in receipt['checks']], ['passed', 'unverified'])
                self.assertIn('parser limit', receipt['checks'][1]['reason'])
                self.assertEqual(self.seal(attempt), receipt)
                history = json.loads(tools.receipts_get({'task_id':'demo', 'attempt_id':attempt}))
                self.assertEqual(history['attempts'], [receipt])
                (self.root / 'nested.json').write_text('{"ok":true}')
                self.assertEqual(self.seal(self.begin(raw))['state'], 'verified')

    def test_json_parser_resource_errors_are_unverified(self):
        raw = {'path':'page.html', 'json_keys':['ok']}
        for error in (MemoryError, OverflowError):
            with self.subTest(error=error.__name__), patch.object(checks.json, 'loads', side_effect=error):
                self.assertEqual(checks.check_file(raw, str(self.root))['state'], 'unverified')

    def test_traversal_and_absolute_paths_rejected(self):
        for value in ("../outside", "/etc/passwd", "a/../page.html", "a//x"):
            with self.assertRaises(schema.SpecError): schema.normalize_spec(self.spec(path=value))

    def test_symlink_and_parent_symlink_rejected(self):
        (self.root / 'link').symlink_to(self.root / 'page.html')
        (self.root / 'directory').symlink_to(self.root, target_is_directory=True)
        for value in ('link', 'directory/page.html'):
            result = checks.check_file({'path':value,'contains':'Ready'}, str(self.root))
            self.assertNotEqual(result['state'], 'passed')

    def test_fifo_directory_and_oversized(self):
        os.mkfifo(self.root / 'pipe')
        (self.root / 'large').write_bytes(b'x' * (schema.FILE_READ_CAP + 1))
        for value in ('pipe', 'state', 'large'):
            result = checks.check_file({'path':value,'contains':'x'}, str(self.root))
            self.assertNotEqual(result['state'], 'passed')

    def test_test_without_observation_unverified(self):
        self.assertEqual(self.seal(self.begin(self.spec('test')))['state'], 'unverified')

    def test_observed_pass(self):
        att = self.begin(self.spec('test')); self.observe()
        self.assertEqual(self.seal(att)['state'], 'verified')

    def test_observed_nonzero_is_failed_even_host_error_classification(self):
        att = self.begin(self.spec('test'))
        self.observe(result={'exit_code':3,'output':'Not ready'}, status='error')
        self.assertEqual(self.seal(att)['state'], 'failed')

    def test_expected_nonzero_can_pass(self):
        att = self.begin(self.spec('test',expected_exit=3))
        self.observe(result={'exit_code':3,'output':'Ready'}, status='error')
        self.assertEqual(self.seal(att)['state'], 'verified')

    def test_background_yield_unverified(self):
        att = self.begin(self.spec('test'))
        self.observe(result={'exit_code':None,'output':'Ready','status':'yielded_to_background'})
        self.assertEqual(self.seal(att)['state'], 'unverified')

    def test_declared_timeout_checked_before_contains(self):
        att = self.begin(self.spec('test',timeout=1)); self.observe(duration=1500)
        self.assertEqual(self.seal(att)['state'], 'unverified')

    def test_native_timeout_unverified(self):
        att = self.begin(self.spec('test'))
        self.observe(result={'exit_code':124,'output':'Ready','error':'timed out'})
        self.assertEqual(self.seal(att)['state'], 'unverified')

    def test_blocked_unverified(self):
        att = self.begin(self.spec('test')); self.observe(status='blocked')
        self.assertEqual(self.seal(att)['state'], 'unverified')

    def test_wrong_host_cannot_seal(self):
        att = self.begin(self.spec('test')); self.observe()
        self.assertFalse(self.seal(att,task_id='other',session_id='host-session')['ok'])
        self.assertFalse(json.loads(tools.receipts_check({'attempt_id':att}))['ok'])
        self.assertEqual(self.seal(att)['state'],'verified')

    def test_wrong_session_does_not_capture(self):
        att = self.begin(self.spec('test')); self.observe(task_id='host-task',session_id='other')
        self.assertEqual(self.seal(att)['state'],'unverified')

    def test_post_without_pre_unverified(self):
        att = self.begin(self.spec('test'))
        observe.on_post_tool_call(tool_name='terminal',args={'command':'printf Ready','workdir':str(self.root)},result={'exit_code':0,'output':'Ready'},tool_call_id='missing-pre',**self.identity)
        self.assertEqual(self.seal(att)['state'],'unverified')

    def test_ambiguous_open_attempt_rejected(self):
        self.begin()
        self.assertFalse(json.loads(tools.receipts_begin({'spec':self.spec()},**self.identity))['ok'])

    def test_restart_expires_capture_and_permits_new_attempt(self):
        att = self.begin(self.spec('test'))
        storage.reset_epoch_for_tests(); self.observe()
        self.assertEqual(self.seal(att)['state'],'unverified')
        self.assertTrue(self.begin())

    def test_restart_keeps_sealed_history(self):
        att = self.begin(); receipt=self.seal(att); storage.reset_epoch_for_tests()
        got=json.loads(tools.receipts_get({'task_id':'demo'}))
        self.assertEqual(got['attempts'][0],receipt)

    def test_freshness_and_digest(self):
        now=datetime.now(timezone.utc); fmt='%Y-%m-%dT%H:%M:%SZ'
        old=(now-timedelta(minutes=10)).strftime(fmt); current=now.strftime(fmt)
        obs={'criteria_digest':'right','started_at':old,'completed_at':old}
        self.assertFalse(tools._fresh(obs,old,current,300,'right'))
        obs.update(started_at=current,completed_at=current)
        self.assertFalse(tools._fresh(obs,current,current,300,'wrong'))
        self.assertTrue(tools._fresh(obs,current,current,300,'right'))

    def test_duplicate_hooks_keep_one_observation(self):
        att=self.begin(self.spec('test'));self.observe();self.observe()
        with storage.transaction(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
        self.assertEqual(self.seal(att)['state'],'verified')

    def test_immutable_historical_failure_and_new_success(self):
        (self.root/'page.html').write_text('Wrong');old=self.begin();failed=self.seal(old)
        (self.root/'page.html').write_text('Ready');new=self.begin();passed=self.seal(new)
        self.assertEqual(failed['state'],'failed');self.assertEqual(passed['state'],'verified')
        self.assertEqual(self.seal(old),failed)
        self.assertEqual(json.loads(tools.receipts_get({'task_id':'demo'}))['count'],2)

    def test_incomplete_multi_check_never_verified(self):
        raw=self.spec();raw['checks'].append({'id':'tests','kind':'test','command':'printf Ready','workdir':str(self.root)})
        self.assertEqual(self.seal(self.begin(raw))['state'],'unverified')

    def test_output_bounded_by_bytes(self):
        parsed=observe.parse_terminal_observation({'exit_code':0,'output':'€'*9000},hook_status='ok',duration_ms=1,error_type=None,error_message=None)
        self.assertLessEqual(len(parsed['output'].encode()),schema.OUTPUT_KEEP_BYTES)
        self.assertTrue(parsed['output_truncated'])

    def test_html_escape_and_secret_canary_redaction(self):
        att=self.begin();receipt=self.seal(att)
        receipt['checks'][0]['reason']='<script>alert(1)</script> token=SYNTHETIC_CANARY'
        receipt['spec']['checks'][0]['contains']='password=SYNTHETIC_CANARY'
        receipt['checks'][0]['evidence']['output']='SYNTHETIC_CANARY'
        rendered=exports.render_html(receipt)
        self.assertNotIn('<script>',rendered);self.assertIn('&lt;script&gt;',rendered)
        self.assertNotIn('SYNTHETIC_CANARY',rendered)
        public=json.loads(exports.render_json(receipt))
        self.assertNotIn('host_session_id',public)
        self.assertEqual(public['spec']['workspace_root'],'<redacted-path>')

    def test_export_path_control_and_consistency(self):
        att=self.begin();self.seal(att)
        self.assertFalse(json.loads(tools.receipts_export({'attempt_id':att,'path':'/outside'}))['ok'])
        for fmt in ('html','json'):
            result=json.loads(tools.receipts_export({'attempt_id':att,'format':fmt}))
            self.assertTrue(result['ok']);self.assertTrue((self.db.parent/'exports'/result['filename']).is_file())

    def test_public_url_redaction_preserves_private_snapshot_and_digest(self):
        urls = (
            'https://demo:USERINFO_CANARY@example.com/',
            'https://example.com/?opaque=QUERY_CANARY#FRAGMENT_CANARY',
            'https://demo:USERINFO_CANARY@example.com/?opaque=QUERY_CANARY#FRAGMENT_CANARY',
        )
        for url in urls:
            with self.subTest(url=url), patch.object(checks, 'fetch_url', side_effect=AssertionError('refused URL fetched')) as fetch:
                att = self.begin(self.spec('website', url=url))
                receipt = self.seal(att)
                self.assertEqual(receipt['state'], 'unverified')
                fetch.assert_not_called()
                before = json.dumps(receipt, sort_keys=True)
                for fmt in ('json', 'html'):
                    result = json.loads(tools.receipts_export({'attempt_id':att, 'format':fmt}))
                    self.assertTrue(result['ok'])
                    rendered = (self.db.parent/'exports'/result['filename']).read_text()
                    for canary in ('USERINFO_CANARY', 'QUERY_CANARY', 'FRAGMENT_CANARY'):
                        self.assertNotIn(canary, rendered)
                    if fmt == 'json':
                        public = json.loads(rendered)
                        self.assertEqual(public['spec']['checks'][0]['url'], 'https://example.com/')
                        self.assertEqual(public['criteria_digest'], receipt['criteria_digest'])
                self.assertEqual(receipt['spec']['checks'][0]['url'], url)
                self.assertEqual(schema.criteria_digest(receipt['spec']), receipt['criteria_digest'])
                self.assertEqual(json.dumps(self.seal(att), sort_keys=True), before)
                self.assertEqual(json.dumps(receipt, sort_keys=True), before)

    def test_public_view_sanitizes_evidence_urls_too(self):
        receipt = self.seal(self.begin())
        receipt['checks'][0]['evidence']['url'] = 'https://demo:URL_CANARY@example.com/?opaque=URL_CANARY#URL_CANARY'
        self.assertNotIn('URL_CANARY', exports.render_json(receipt))
        self.assertNotIn('URL_CANARY', exports.render_html(receipt))

    def test_export_refuses_symlink(self):
        receipt=self.seal(self.begin());directory=self.root/'exports';directory.mkdir()
        target=directory/('receipt-'+receipt['attempt_id']+'.json')
        target.symlink_to(self.root/'page.html')
        with self.assertRaises(OSError):exports.write_export(receipt,directory,fmt='json')
        self.assertEqual((self.root/'page.html').read_text(),'Ready')

    def test_aggregate_semantics(self):
        for states,want in (([], 'unverified'),(['passed','unverified'],'unverified'),(['failed','unverified'],'failed'),(['passed'],'verified')):
            self.assertEqual(schema.aggregate_state(states),want)

    def test_profile_override_isolation(self):
        self.seal(self.begin());tools.set_db_override(self.root/'other-profile'/'data.db')
        self.assertEqual(json.loads(tools.receipts_get({'task_id':'demo'}))['count'],0)


if __name__=='__main__':unittest.main()
