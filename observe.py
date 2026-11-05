"""Observe armed terminal executions. Never dispatch a command from here."""

from __future__ import annotations

import json
from typing import Any

from .schema import OUTPUT_KEEP_BYTES, host_context, host_context_complete
from .storage import (
    bind_host_if_missing,
    get_attempt,
    get_pre_event,
    insert_observation,
    insert_pre_event,
    match_armed,
    transaction,
    utc_now,
)

RECEIPT_TOOLS = frozenset({"receipts_begin", "receipts_check", "receipts_get", "receipts_export"})
_UNVERIFIED_STATUSES = frozenset({
    "blocked", "cancelled", "canceled", "pending_approval", "denied", "degraded",
    "yielded_to_background", "timeout", "error",
})


def on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    task_id: str | None = None,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> None:
    """Record a matching terminal start. Never rewrite arguments or approve."""
    del kwargs
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = args.get("command")
    workdir = args.get("workdir")
    if not isinstance(command, str) or not isinstance(workdir, str) or not tool_call_id:
        return None
    host_task, host_session = host_context(task_id, session_id)
    if not host_context_complete(host_task, host_session):
        return None
    with transaction() as conn:
        matches = match_armed(conn, command=command, workdir=workdir, host_task_id=host_task, host_session_id=host_session)
        if len(matches) != 1:
            return None
        armed = matches[0]
        attempt = get_attempt(conn, armed["attempt_id"])
        if attempt is None or attempt["state"] != "open":
            return None
        insert_pre_event(conn, {
            "tool_call_id": tool_call_id,
            "attempt_id": armed["attempt_id"],
            "check_id": armed["check_id"],
            "command": command,
            "workdir": workdir,
            "host_task_id": host_task,
            "host_session_id": host_session,
            "started_at": utc_now(),
        })
    return None


def on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str | None = None,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    **kwargs: Any,
) -> None:
    """Ingest one armed terminal completion, or bind receipt-tool host identity."""
    del kwargs
    host_task, host_session = host_context(task_id, session_id)
    if tool_name in RECEIPT_TOOLS:
        _bind_receipt_identity(result, host_task, host_session)
        return None
    if tool_name != "terminal" or not tool_call_id or not isinstance(args, dict):
        return None
    with transaction() as conn:
        pre = get_pre_event(conn, tool_call_id)
        if pre is None:
            return None
        attempt = get_attempt(conn, pre["attempt_id"])
        if attempt is None or attempt["state"] != "open":
            return None
        if pre["command"] != args.get("command") or pre["workdir"] != args.get("workdir"):
            return None
        if pre["host_task_id"] != host_task or pre["host_session_id"] != host_session:
            return None
        payload = parse_terminal_observation(
            result,
            hook_status=status,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
        )
        payload.update({
            "tool_call_id": tool_call_id,
            "attempt_id": pre["attempt_id"],
            "check_id": pre["check_id"],
            "command": pre["command"],
            "workdir": pre["workdir"],
            "host_task_id": host_task,
            "host_session_id": host_session,
            "criteria_digest": attempt["criteria_digest"],
            "started_at": pre["started_at"],
            "completed_at": utc_now(),
            "missing_pre": False,
        })
        insert_observation(conn, tool_call_id, pre["attempt_id"], pre["check_id"], payload)
    return None


def _bind_receipt_identity(result: Any, host_task: str | None, host_session: str | None) -> None:
    attempt_id = _attempt_id_from_result(result)
    if not attempt_id:
        return
    with transaction() as conn:
        if host_task or host_session:
            bind_host_if_missing(conn, attempt_id, host_task, host_session)


def _attempt_id_from_result(result: Any) -> str | None:
    text = result if isinstance(result, str) else ""
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    attempt_id = payload.get("attempt_id")
    return attempt_id if isinstance(attempt_id, str) else None


def parse_terminal_observation(
    result: Any,
    *,
    hook_status: str | None,
    duration_ms: int | None,
    error_type: str | None,
    error_message: str | None,
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    malformed = False
    if isinstance(result, str):
        try:
            loaded = json.loads(result)
            if isinstance(loaded, dict):
                parsed = loaded
            else:
                malformed = True
        except json.JSONDecodeError:
            malformed = True
    elif isinstance(result, dict):
        parsed = result
    else:
        malformed = True
    output = parsed.get("output") if isinstance(parsed.get("output"), str) else ""
    encoded = output.encode("utf-8")
    truncated = len(encoded) > OUTPUT_KEEP_BYTES or bool(parsed.get("truncation_note") or parsed.get("output_total_chars"))
    kept = encoded[:OUTPUT_KEEP_BYTES].decode("utf-8", errors="ignore")
    exit_code = parsed.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    error = parsed.get("error")
    if not isinstance(error, str):
        # The host can classify an ordinary nonzero process exit as a tool
        # failure. That remains real failing evidence, not a checker error.
        error = error_message if exit_code is None and isinstance(error_message, str) else ""
    return {
        "malformed": malformed,
        "hook_status": hook_status,
        "terminal_status": parsed.get("status") if isinstance(parsed.get("status"), str) else None,
        "exit_code": exit_code,
        "output": kept,
        "output_truncated": truncated,
        "error": error[:240],
        "error_type": error_type,
        "duration_ms": duration_ms if isinstance(duration_ms, int) else None,
        "provenance": "observed-native-terminal",
    }


def judge_test_observation(check: dict[str, Any], observation: dict[str, Any] | None, *, fresh: bool, identity_ok: bool) -> dict[str, Any]:
    evidence = {"provenance_note": "observed native terminal execution, not a human approval"}
    if observation is None:
        return _gap("no observed terminal execution for this attempt", evidence)
    evidence.update({
        "tool_call_id": observation.get("tool_call_id"),
        "exit_code": observation.get("exit_code"),
        "output_truncated": bool(observation.get("output_truncated")),
        "duration_ms": observation.get("duration_ms"),
        "terminal_status": observation.get("terminal_status"),
    })
    if not identity_ok:
        return _gap("host identity missing or conflicting", evidence)
    if not fresh:
        return _gap("terminal observation is stale, early, or incomplete", evidence)
    if observation.get("malformed"):
        return _gap("malformed terminal result", evidence)
    hook_status = str(observation.get("hook_status") or "").lower()
    terminal_status = str(observation.get("terminal_status") or "").lower()
    if hook_status in {"blocked", "cancelled", "canceled"} or terminal_status in _UNVERIFIED_STATUSES:
        return _gap(f"terminal not completed cleanly ({terminal_status or hook_status})", evidence)
    exit_code = observation.get("exit_code")
    if exit_code is None:
        return _gap("missing exit code", evidence)
    error = str(observation.get("error") or "")
    if exit_code == 124 or "timed out" in error.lower():
        return _gap("timeout", evidence)
    if error:
        return _gap("terminal reported an execution error", evidence)
    duration = observation.get("duration_ms")
    if isinstance(duration, int) and duration > check["timeout"] * 1000:
        return _gap("observation exceeded the declared timeout", evidence)
    output = observation.get("output") if isinstance(observation.get("output"), str) else ""
    if exit_code != check["expected_exit"]:
        return {
            "state": "failed",
            "reason": f"exit {exit_code} != expected {check['expected_exit']}",
            "evidence": evidence,
            "provenance": "observed-native-terminal",
        }
    needle = check.get("contains")
    if needle:
        if needle in output:
            return _pass("exit and required output matched", evidence)
        if observation.get("output_truncated"):
            return _gap("output truncated before required text could be confirmed", evidence)
        return {
            "state": "failed",
            "reason": "required output absent",
            "evidence": evidence,
            "provenance": "observed-native-terminal",
        }
    return _pass("exit matched", evidence)


def _pass(reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"state": "passed", "reason": reason, "evidence": evidence, "provenance": "observed-native-terminal"}


def _gap(reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"state": "unverified", "reason": reason, "evidence": evidence, "provenance": "observed-native-terminal"}
