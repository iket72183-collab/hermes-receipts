# Hermes Receipts

**Your agent says it finished. See what actually passed.**

A Hermes Agent plugin by **BDT Talent Group**. Define explicit checks before a task runs, then preserve inspectable, timestamped evidence of **verified**, **failed**, or **unverified** outcomes. Files are read, websites are fetched, and matching native terminal executions are observed. No additional model, API key, hosted service, or paid API is required.

## What gets checked

- **Files:** a workspace-relative regular file contains required text, or is a JSON object with required keys. Evidence includes the size and SHA-256 of the bytes checked.
- **Tests:** an armed exact command/workdir match was observed through native Hermes terminal hooks with the required exit code and optional output. Receipts never executes commands itself or changes approvals.
- **Websites:** a scoped GET returns both the expected HTTP status and literal content. HTTP 200 alone is not success.

**Verified:** every specified check passed. **Failed:** at least one requirement actually failed. **Unverified:** evidence is missing, expired, incomplete, timed out, or could not safely be checked. A later success never overwrites an earlier failed attempt.

## Install

Requires Hermes **0.22.4+**, Python **3.11+**, and Linux or macOS. The file adapter uses POSIX descriptor-relative reads; Windows is not supported in this version. Until the catalog PR is accepted by a maintainer, install from the repository:

```sh
hermes plugins install https://github.com/iket72183-collab/hermes-receipts
hermes plugins enable hermes-receipts
```

Start a new Hermes process after installation. No extra Python dependencies or credentials are needed by the plugin. The offline development harness additionally requires a Hermes source checkout and its normal dependencies.

The unit suite is tested on Python 3.11 and 3.14. Valid deeply nested JSON can parse successfully on one Python version and exceed parser limits on another; parser limits produce an unverified check and still seal the attempt.

## Use with Hermes

Ask Hermes to call `receipts_begin` **before** performing the task. Example arguments:

```json
{
  "spec": {
    "schema_version": 1,
    "task_id": "webpage-delivery",
    "workspace_root": "/absolute/path/to/project",
    "checks": [
      {"id": "page", "kind": "file", "path": "index.html", "contains": "Ready to launch"},
      {"id": "tests", "kind": "test", "command": "python3 verify_page.py", "workdir": "/absolute/path/to/project", "expected_exit": 0, "contains": "PASSED", "timeout": 30},
      {"id": "live-page", "kind": "website", "url": "https://example.com/", "expected_status": 200, "contains": "Ready to launch"}
    ]
  }
}
```

1. Perform the real work. Run the exact test command with its explicit `workdir` through the ordinary Hermes `terminal` tool. Normal host approval rules still apply.
2. Call `receipts_check` with the returned `attempt_id`. This reads files/websites now, evaluates the armed terminal observation, and seals an immutable snapshot.
3. Retrieve it with `receipts_get`, or `/receipts webpage-delivery [attempt-id]`.
4. Call `receipts_export` with `attempt_id` and `format: "html"` or `"json"`. The response gives a generated filename under the active profile's `plugin-data/hermes-receipts/exports/` directory.

One open attempt is allowed per host task/session in the capturing process. Checks and criteria digests are immutable: fix the actual task, then begin a **new** attempt. Each spec may contain only one test check per exact command/workdir pair; duplicates are rejected at begin even when their expectations differ. Test observations must match host identity, command/workdir, process capture, and the freshness window (default 300 seconds). Missing host identity cannot verify a test. Background yields, denied actions, timeouts, and model-supplied exit codes do not pass. If a matching test command runs more than once in an open attempt, any failed run seals as failed and any incomplete or unverified run seals as unverified; an earlier passing run never masks a later failure or in-flight execution. Looking up receipts from another process does not invalidate a live capture. Another process, including a restarted or forked process, cannot continue that capture; start a fresh attempt. History remains readable.

Paths containing NUL characters are rejected before capture begins. Older persisted attempts with malformed paths and file/website adapter errors seal as unverified while preserving the results of other checks, so a new attempt can begin.

Optional CLI: `hermes receipts begin --spec spec.json`, `check --attempt-id ID`, `get --task-id LABEL`, and `export --attempt-id ID --format html`. CLI file/website checks work normally; standalone CLI calls cannot manufacture the host identity required for terminal evidence.

## Real demo and checks

The demo deliberately serves the wrong headline. File, native test, and website checks fail, although HTTP returns 200. It corrects the actual file, begins a fresh attempt, reruns all checks, and saves a verified receipt while retaining the failure. Fault injection is disclosed; no passing receipts or model responses are fabricated.

From this checkout with Hermes dependencies available:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/smoke_plugin.py --hermes-source /path/to/hermes-agent --evidence-dir output/smoke
python3 scripts/demo.py --hermes-source /path/to/hermes-agent --output-dir output/demo
python3 scripts/read_safety_smoke.py --hermes-source /path/to/hermes-agent --evidence-dir output/read-safety
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
git diff --check
```

Add `--interactive` to pause the real demo at its execution stages for recording. The scripts create a fresh isolated Hermes home, load through the actual PluginManager, and use real native dispatch. Smoke scenarios include passing, failing, and timed-out native commands. The demo starts and shuts down only its own loopback server. Neither script calls a model or disables approval policy.

## Limits and disclosures

- **As-of snapshot, not continuous certification.** Only listed checks are covered. The receipt does not prove the criteria are sufficient or the whole task is correct.
- **Not a security sandbox, lie detector, or signed attestation.** A malicious host/plugin can tamper with local state or hook evidence. Digests identify checked content; they are not signatures.
- **File reads:** explicitly selected files within the supplied workspace only. Workspaces at or under the active `get_hermes_home()` are refused (including resolved aliases). Before opening a file, the adapter calls Hermes’ `agent.file_safety.get_read_block_error` with its absolute path; denied reads or unavailable safety helpers are **unverified**, with a reason and no content-derived evidence. This also applies when sealing older attempts. Symlink components, traversal, directories, devices, and FIFOs are rejected; reads are capped at 1 MiB. Malformed JSON fails; parser depth, integer conversion, or resource limits remain unverified without discarding other check results. The plugin never writes project files.
- **Network:** GETs explicit public HTTPS URLs, with a five-second deadline and 1 MiB body cap. Complete HTTP framing is required before content can pass; early EOF and deadline termination remain unverified. Redirects are not followed; compressed responses, URL credentials, and query strings are refused. Resolved private/link-local/metadata targets are refused; connections pin a checked address. Loopback HTTP requires explicit `allow_loopback_http: true` for a controlled demo. No environment proxies or authentication headers. A timed-out OS DNS lookup can finish in a daemon thread, but cannot cause a later fetch.
- **Terminal observation:** stores only explicitly armed matching executions, not general terminal history. Commands, bounded output (8 KiB), exit codes, timestamps, and host IDs remain local. Native execution is not necessarily human-approved; host policy decides. Receipt handlers do not spawn shells, execute commands, or bypass guards.
- **Storage:** the active profile's `plugin-data/hermes-receipts/`, never the install tree. Exports stay in its `exports/` directory; arbitrary write destinations are refused. No telemetry, credential reads, external receipt service, or registration-time networking.
- **Public exports:** omit raw command output, host IDs, commands, and workspace/workdir paths; strip URL userinfo, query strings, and fragments from both criteria and evidence. The digest still identifies the original private immutable criteria, not the redacted public spec. HTML escapes untrusted text and contains no scripts or remote assets. Redaction is best-effort, not a confidentiality guarantee: review before sharing. Public demos use synthetic data. Local lookup returns full private receipt details.
- **Deletion:** stop processes using the plugin, then remove the active profile's `plugin-data/hermes-receipts/` directory through your file manager. Uninstalling the code does not remove receipts or copies exported elsewhere.

Production registration uses public `ctx.register_*` APIs, public hooks, and `plugins.plugin_storage`. File reads additionally delegate to the core `agent.file_safety.get_read_block_error` guard as requested by catalog review; the validator flags this required safety import as private. Offline acceptance scripts use host dispatch infrastructure for testing; they are not imported by the plugin. The catalog template in `catalog/hermes-receipts.yaml` must be filled with the actual public repo and reviewed commit, never a placeholder pin.

## License

MIT. Built by BDT Talent Group for the Hermes Agent Plugin Hackathon. Community project; not an official Nous Research plugin.
