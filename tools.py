"""Receipt tools. Host identity comes from dispatcher kwargs, never from the spec."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import check_file, check_website, workspace_refusal
from .export import render_html, render_json, write_export
from .observe import judge_test_observation
from .schema import (
    PLUGIN_NAME,
    REJECTED_EVIDENCE_KEYS,
    SpecError,
    aggregate_state,
    criteria_digest,
    host_context,
    host_context_complete,
    valid_id,
)
from .storage import (
    get_attempt,
    insert_attempt,
    list_attempts,
    new_attempt_id,
    observation_for,
    open_attempt_for_host,
    seal_attempt,
    transaction,
    utc_now,
)

DISCLOSURES = [
    "As-of snapshot of explicit checks, not ongoing certification.",
    "Not a lie detector, security sandbox, or signed attestation.",
    "Observed native terminal execution is not a human approval.",
    "Hook evidence is not cryptographically trustworthy against a malicious host.",
    "No telemetry. Delete the active profile plugin-data/hermes-receipts directory to remove local state.",
]

_DB_OVERRIDE: Path | None = None


def set_db_override(path: Path | None) -> None:
    global _DB_OVERRIDE
    _DB_OVERRIDE = path


def receipts_begin(
    args: dict[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> str:
    del tool_call_id, kwargs
    if not isinstance(args, dict) or not isinstance(args.get("spec"), dict):
        return _error("spec object is required")
    try:
        spec = normalize(args["spec"])
    except (SpecError, TypeError, ValueError, RecursionError) as exc:
        return _error(str(exc))
    root = Path(spec["workspace_root"])
    refusal = workspace_refusal(str(root))
    if refusal:
        return _error(refusal)
    if not root.is_dir():
        return _error("workspace_root is not an existing directory")
    host_task, host_session = host_context(task_id, session_id)
    digest = criteria_digest(spec)
    started = utc_now()
    attempt_id = new_attempt_id()
    with transaction(_DB_OVERRIDE) as conn:
        if host_context_complete(host_task, host_session):
            existing = open_attempt_for_host(conn, host_task, host_session)
            if existing:
                return _error(f"ambiguous open attempt in this host context: {existing}")
        insert_attempt(
            conn,
            attempt_id=attempt_id,
            task_id=spec["task_id"],
            host_task_id=host_task,
            host_session_id=host_session,
            digest=digest,
            spec=spec,
            started_at=started,
        )
    return _ok(
        attempt_id=attempt_id,
        task_id=spec["task_id"],
        criteria_digest=digest,
        started_at=started,
        state="open",
        host_bound=host_context_complete(host_task, host_session),
        terminal_evidence="verifiable" if host_context_complete(host_task, host_session) else "unverified_without_host_identity",
    )


def receipts_check(
    args: dict[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> str:
    del tool_call_id, kwargs
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    leaked = sorted(REJECTED_EVIDENCE_KEYS & set(args))
    if leaked:
        return _error("model-supplied evidence is rejected")
    attempt_id = args.get("attempt_id")
    if not valid_id(attempt_id):
        return _error("attempt_id is invalid")
    with transaction(_DB_OVERRIDE) as conn:
        row = get_attempt(conn, attempt_id)
        if row is None:
            return _error("attempt not found")
        if row["receipt_json"]:
            return row["receipt_json"]
        spec = json.loads(row["spec_json"])
        stored_task = row["host_task_id"]
        stored_session = row["host_session_id"]
        conflict = bool(row["identity_conflict"])
    host_task, host_session = host_context(task_id, session_id)
    if host_context_complete(stored_task, stored_session):
        if host_task != stored_task or host_session != stored_session:
            return _error("bound attempt requires the same host task and session to check")
    evaluated: dict[str, dict[str, Any]] = {}
    for check in spec["checks"]:
        if check["kind"] == "file":
            evaluated[check["id"]] = check_file(check, spec["workspace_root"])
        elif check["kind"] == "website":
            evaluated[check["id"]] = check_website(check, allow_loopback_http=bool(spec["allow_loopback_http"]))
    checked_at = utc_now()
    with transaction(_DB_OVERRIDE) as conn:
        row = get_attempt(conn, attempt_id)
        if row is None:
            return _error("attempt not found")
        if row["receipt_json"]:
            return row["receipt_json"]
        checks_out = []
        for check in spec["checks"]:
            if check["kind"] == "test":
                result = _evaluate_test(conn, row, check, spec, checked_at, conflict)
            else:
                result = evaluated[check["id"]]
            checks_out.append({"id": check["id"], "kind": check["kind"], **result})
        state = aggregate_state([item["state"] for item in checks_out])
        receipt = {
            "schema_version": 1,
            "task_id": row["task_id"],
            "attempt_id": attempt_id,
            "host_task_id": row["host_task_id"],
            "host_session_id": row["host_session_id"],
            "criteria_digest": row["criteria_digest"],
            "spec": spec,
            "started_at": row["started_at"],
            "checked_at": checked_at,
            "state": state,
            "checks": checks_out,
            "disclosures": DISCLOSURES,
        }
        seal_attempt(conn, attempt_id, receipt, checked_at)
        return json.dumps(receipt, sort_keys=True)


def receipts_get(
    args: dict[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    **kwargs: Any,
) -> str:
    del task_id, session_id, kwargs
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    label = args.get("task_id")
    if not valid_id(label):
        return _error("task_id is invalid")
    attempt_id = args.get("attempt_id")
    if attempt_id is not None and not valid_id(attempt_id):
        return _error("attempt_id is invalid")
    with transaction(_DB_OVERRIDE) as conn:
        rows = list_attempts(conn, label)
        if attempt_id is not None:
            rows = [row for row in rows if row["attempt_id"] == attempt_id]
        attempts = [_public_row(row) for row in rows]
    return _ok(task_id=label, attempts=attempts, count=len(attempts))


def receipts_export(
    args: dict[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    **kwargs: Any,
) -> str:
    del task_id, session_id, kwargs
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    if any(key in args for key in ("path", "filename", "output_path", "dest")):
        return _error("export path is not caller-controlled")
    attempt_id = args.get("attempt_id")
    if not valid_id(attempt_id) or ".." in str(args.get("format", "")):
        return _error("refusing unsafe export request")
    fmt = args.get("format", "json")
    if fmt not in {"json", "html"}:
        return _error("format must be json or html")
    public = args.get("public", True)
    if not isinstance(public, bool):
        return _error("public must be a boolean")
    with transaction(_DB_OVERRIDE) as conn:
        row = get_attempt(conn, attempt_id)
        if row is None or not row["receipt_json"]:
            return _error("sealed receipt not found")
        receipt = json.loads(row["receipt_json"])
    text = render_json(receipt, public=public) if fmt == "json" else render_html(receipt, public=public)
    try:
        path = write_export(receipt, _export_dir(), fmt=fmt, public=public)
    except ValueError as exc:
        return _error(str(exc))
    return _ok(attempt_id=attempt_id, format=fmt, filename=path.name, bytes=len(text.encode("utf-8")), public=public)


def slash_receipts(raw_args: str = "", **kwargs: Any) -> str:
    del kwargs
    parts = (raw_args or "").split()
    if not parts or len(parts) > 2:
        return "usage: /receipts <task-id> [attempt-id]"
    payload = {"task_id": parts[0]}
    if len(parts) == 2:
        payload["attempt_id"] = parts[1]
    parsed = json.loads(receipts_get(payload))
    if not parsed.get("ok"):
        return parsed.get("error", "receipt lookup failed")
    lines = [f"Receipts for {parts[0]}: {parsed['count']}"]
    for item in parsed["attempts"]:
        lines.append(f"- {item['attempt_id']} {item['state']} checked={item.get('checked_at') or '-'}")
    return "\n".join(lines)


def setup_cli(parser: Any) -> None:
    sub = parser.add_subparsers(dest="receipts_cmd", required=True)
    begin = sub.add_parser("begin", help="Begin an attempt from a spec JSON file")
    begin.add_argument("--spec", required=True)
    check = sub.add_parser("check", help="Run file and website checks and seal observed tests")
    check.add_argument("--attempt-id", required=True)
    get = sub.add_parser("get", help="Read persisted receipts")
    get.add_argument("--task-id", required=True)
    get.add_argument("--attempt-id")
    export = sub.add_parser("export", help="Write a plugin-owned JSON or HTML export")
    export.add_argument("--attempt-id", required=True)
    export.add_argument("--format", choices=("json", "html"), default="json")


def cli_main(args: Any) -> None:
    command = getattr(args, "receipts_cmd", None)
    if command == "begin":
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        print(receipts_begin({"spec": spec}))
        return
    if command == "check":
        print(receipts_check({"attempt_id": args.attempt_id}))
        return
    if command == "get":
        payload: dict[str, Any] = {"task_id": args.task_id}
        if args.attempt_id:
            payload["attempt_id"] = args.attempt_id
        print(receipts_get(payload))
        return
    if command == "export":
        print(receipts_export({"attempt_id": args.attempt_id, "format": args.format, "public": True}))
        return
    raise SystemExit("usage: hermes receipts begin|check|get|export ...")


def normalize(raw: dict[str, Any]):
    from .schema import normalize_spec
    return normalize_spec(raw)


def _evaluate_test(conn, row, check, spec, checked_at: str, conflict: bool) -> dict[str, Any]:
    identity_ok = host_context_complete(row["host_task_id"], row["host_session_id"]) and not conflict
    observation = observation_for(conn, row["attempt_id"], check["id"])
    fresh = False
    if observation is not None and identity_ok:
        fresh = _fresh(observation, row["started_at"], checked_at, spec["freshness_seconds"], row["criteria_digest"])
    return judge_test_observation(check, observation, fresh=fresh, identity_ok=identity_ok)


def _fresh(observation: dict[str, Any], started_at: str, checked_at: str, window: int, digest: str) -> bool:
    if observation.get("criteria_digest") != digest:
        return False
    obs_started = observation.get("started_at")
    obs_done = observation.get("completed_at")
    if not isinstance(obs_started, str) or not isinstance(obs_done, str):
        return False
    if obs_started < started_at or obs_done > checked_at:
        return False
    try:
        done = datetime.strptime(obs_done, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        sealed = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (sealed - done).total_seconds() <= window


def _export_dir() -> Path:
    if _DB_OVERRIDE is not None:
        path = _DB_OVERRIDE.parent / "exports"
    else:
        from plugins.plugin_storage import plugin_data_dir
        path = plugin_data_dir(PLUGIN_NAME) / "exports"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _public_row(row) -> dict[str, Any]:
    if row["receipt_json"]:
        return json.loads(row["receipt_json"])
    return {
        "schema_version": 1,
        "task_id": row["task_id"],
        "attempt_id": row["attempt_id"],
        "criteria_digest": row["criteria_digest"],
        "started_at": row["started_at"],
        "checked_at": None,
        "state": "open",
        "spec": json.loads(row["spec_json"]),
    }


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, sort_keys=True)


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, sort_keys=True)
