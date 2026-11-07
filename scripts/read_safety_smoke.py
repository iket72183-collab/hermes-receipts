"""Offline regression against real Hermes safety and native plugin dispatch."""
import argparse
import json
from pathlib import Path

from native_runtime import NativeRuntime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    runtime = NativeRuntime(args.hermes_source, args.evidence_dir)
    from agent.file_safety import get_read_block_error

    (runtime.workspace / 'page.txt').write_text('Ready')
    (runtime.workspace / '.env').write_text('SYNTHETIC_SECRET=Ready')
    (runtime.workspace / '.env.example').write_text('Ready')
    (runtime.home / '.env').write_text('SYNTHETIC_SECRET=Ready')
    child = runtime.home / 'child'
    child.mkdir()
    alias = runtime.workspace / 'profile-alias'
    alias.symlink_to(runtime.home, target_is_directory=True)
    results = []

    def spec(root, path, expectation):
        return {'schema_version': 1, 'task_id': 'read-safety', 'workspace_root': str(root),
                'checks': [{'id': 'file', 'kind': 'file', 'path': path, **expectation}]}

    for root in (runtime.home, child, alias, child / '..'):
        result = runtime.call('receipts_begin', {'spec': spec(root, '.env', {'contains': 'Ready'})})
        assert not result['ok'] and 'Hermes home' in result['error'], result
        results.append({'scenario': 'private-workspace-refused', 'root': str(root)})

    scenarios = [
        (runtime.workspace, 'page.txt', {'contains': 'Ready'}, 'verified'),
        (runtime.workspace, '.env.example', {'contains': 'Ready'}, 'verified'),
        (runtime.workspace, '.env', {'contains': 'Ready'}, 'unverified'),
        (runtime.workspace, '.env', {'contains': 'Wrong'}, 'unverified'),
        (runtime.workspace, '.env', {'json_keys': ['ok']}, 'unverified'),
        # An ancestor workspace cannot bypass the denylist on a private target.
        (runtime.output, str(runtime.home.relative_to(runtime.output) / '.env'), {'contains': 'Ready'}, 'unverified'),
        # Non-denylisted files under HERMES_HOME are also refused via ancestor workspaces.
        (runtime.output, str(runtime.home.relative_to(runtime.output) / 'config.yaml'), {'contains': 'plugins'}, 'unverified'),
    ]
    for root, path, expectation, want in scenarios:
        begin = runtime.call('receipts_begin', {'spec': spec(root, path, expectation)})
        assert begin.get('ok'), begin
        receipt = runtime.call('receipts_check', {'attempt_id': begin['attempt_id']})
        assert receipt['state'] == want, receipt
        check = receipt['checks'][0]
        if want == 'unverified':
            expected_reason = get_read_block_error(str(root / path)) or 'workspace_root must not be at or under the active Hermes home'
            assert check['reason'] == expected_reason, check
            assert check['evidence'] == {'relative_path': path}, check
        results.append({'scenario': path, 'expectation': expectation, 'state': receipt['state']})
    summary = {'real_hermes_safety': True, 'native_dispatch': True, 'synthetic_files_only': True,
               'model_calls': 0, 'scenarios': results}
    (args.evidence_dir / 'read-safety-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
