"""Offline acceptance harness: real Hermes loader, dispatch, hooks and terminal.

This is demo/test infrastructure, never imported by the production plugin.
No model calls, approval overrides, live profile data, or callback seeding.
"""
from __future__ import annotations
import json
import hashlib
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NativeRuntime:
    def __init__(self, hermes_source: Path, output: Path):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.home = self.output / ("home_" + uuid.uuid4().hex[:10])
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(parents=True)
        os.environ['HERMES_HOME'] = str(self.home)
        os.environ.pop('HERMES_PROFILE', None)
        os.environ['TERMINAL_ENV'] = 'local'
        os.environ['TERMINAL_CWD'] = str(self.workspace)
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        os.environ['TMPDIR'] = str(self.home / 'tmp')
        Path(os.environ['TMPDIR']).mkdir()
        installed = self.home / 'plugins' / 'hermes-receipts'
        shutil.copytree(ROOT, installed, ignore=shutil.ignore_patterns('.git', '.worktrees', 'output', '__pycache__', '.venv'))
        # Fresh explicit configuration; native approval policy is not disabled.
        (self.home/'config.yaml').write_text('plugins:\n  enabled: [hermes-receipts]\nterminal:\n  backend: local\n  cwd: '+json.dumps(str(self.workspace))+'\n')
        sys.path.insert(0, str(hermes_source.resolve()))
        from hermes_cli.plugins import get_plugin_manager
        self.manager = get_plugin_manager()
        self.manager.discover_and_load()
        loaded = [p for p in self.manager.list_plugins() if p.get('name') == 'hermes-receipts']
        if not loaded or not loaded[0].get('enabled') or loaded[0].get('error'):
            raise RuntimeError('Plugin discovery failed: '+json.dumps(loaded))
        from model_tools import handle_function_call
        import model_tools
        self.dispatch = handle_function_call
        native_module = Path(model_tools.__file__).resolve()
        if not native_module.is_relative_to(hermes_source.resolve()):
            raise RuntimeError('Native dispatch imported outside the requested Hermes source')
        (self.output/'runtime-provenance.json').write_text(json.dumps({'hermes_source':str(hermes_source.resolve()),'native_dispatch_module':str(native_module),'native_dispatch_sha256':hashlib.sha256(native_module.read_bytes()).hexdigest(),'plugin_source':str(ROOT),'fresh_home':str(self.home),'approval_overrides':False,'model_calls':0},indent=2)+'\n')
        self.task_id = 'receipts-demo-' + uuid.uuid4().hex[:8]
        self.session_id = 'offline-' + uuid.uuid4().hex[:12]
        self.actions = []
        self.logfile = self.output/'native-actions.jsonl'
        self.call('receipts_get', {'task_id':'bootstrap'})

    def call(self, name, arguments):
        stamp = datetime.now(timezone.utc).isoformat()
        call_id = 'receipt-call-' + uuid.uuid4().hex
        result = self.dispatch(name, arguments, task_id=self.task_id, session_id=self.session_id, tool_call_id=call_id, turn_id='offline-acceptance')
        parsed = json.loads(result) if isinstance(result,str) else result
        record = {'at':stamp,'tool':name,'tool_call_id':call_id,'arguments':arguments,'result':parsed,'provenance':'real Hermes handle_function_call; no model or synthetic callback result'}
        self.actions.append(record)
        with self.logfile.open('a') as stream:stream.write(json.dumps(record)+'\n')
        return parsed

    def exports(self, attempt):
        result = {}
        for fmt in ('json','html'):
            value = self.call('receipts_export', {'attempt_id':attempt,'format':fmt,'public':True})
            assert value.get('ok'), value
            origin=self.home/'plugin-data'/'hermes-receipts'/'exports'/value['filename']
            destination=self.output/value['filename']
            shutil.copyfile(origin,destination)
            result[fmt]=str(destination)
        return result


def default_source():
    value=os.environ.get('HERMES_SOURCE')
    if value:return Path(value)
    raise SystemExit('Pass --hermes-source /path/to/clean/hermes-agent or set HERMES_SOURCE')
