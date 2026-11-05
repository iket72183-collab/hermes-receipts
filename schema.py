"""Bounded receipt specs, identity rules, and aggregate state.

No I/O. IDs are validated strings and are never treated as filesystem paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = 1
PLUGIN_NAME = "hermes-receipts"
PLUGIN_VERSION = "0.1.0"

MIN_CHECKS = 1
MAX_CHECKS = 20
MAX_SPEC_BYTES = 64 * 1024
MAX_EXPECTATION = 4096
MAX_COMMAND = 1024
MAX_PATH = 4096
MAX_URL = 2048
MAX_JSON_KEYS = 20
MAX_KEY_LEN = 128
DEFAULT_FRESHNESS_SECONDS = 300
MIN_FRESHNESS_SECONDS = 1
MAX_FRESHNESS_SECONDS = 3600
MIN_TIMEOUT = 1
MAX_TIMEOUT = 120
OUTPUT_KEEP_BYTES = 8 * 1024
FILE_READ_CAP = 1 * 1024 * 1024
WEBSITE_DEADLINE_SECONDS = 5.0

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHECK_KINDS = frozenset({"file", "test", "website"})
CHECK_STATES = frozenset({"passed", "failed", "unverified"})
AGGREGATE_STATES = frozenset({"verified", "failed", "unverified"})

# Model-supplied evidence must never be accepted as a check result.
REJECTED_EVIDENCE_KEYS = frozenset({
    "exit_code", "output", "evidence", "result", "observations", "observation",
    "host_task_id", "host_session_id", "tool_call_id",
})


class SpecError(ValueError):
    """The caller spec is outside the bounded contract."""


def valid_id(value: Any) -> bool:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        return False
    if ".." in value or "/" in value or "\\" in value:
        return False
    return True


def require_id(value: Any, label: str) -> str:
    if not valid_id(value):
        raise SpecError(f"{label} must be a short validated id, not a path")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def criteria_digest(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_str(value: Any, label: str, limit: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SpecError(f"{label} must be a string")
    if len(value) > limit:
        raise SpecError(f"{label} exceeds {limit} characters")
    if required and not value:
        raise SpecError(f"{label} is required")
    return value


def _int(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{label} must be an integer")
    if value < low or value > high:
        raise SpecError(f"{label} must be between {low} and {high}")
    return value


def normalize_spec(raw: Any) -> dict[str, Any]:
    """Return the canonical spec or raise SpecError. Does not touch the filesystem."""
    if not isinstance(raw, dict):
        raise SpecError("spec must be an object")
    encoded = canonical_json(raw).encode("utf-8")
    if len(encoded) > MAX_SPEC_BYTES:
        raise SpecError("spec exceeds 64 KiB")
    allowed = {
        "schema_version", "task_id", "workspace_root", "checks",
        "freshness_seconds", "allow_loopback_http",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SpecError(f"unknown spec fields: {', '.join(unknown)}")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA_VERSION:
        raise SpecError("schema_version must be 1")
    task_id = require_id(raw.get("task_id"), "task_id")
    workspace = raw.get("workspace_root")
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        raise SpecError("workspace_root must be an absolute path")
    if len(workspace) > MAX_PATH:
        raise SpecError("workspace_root is too long")
    freshness = raw.get("freshness_seconds", DEFAULT_FRESHNESS_SECONDS)
    freshness = _int(freshness, "freshness_seconds", MIN_FRESHNESS_SECONDS, MAX_FRESHNESS_SECONDS)
    allow_loopback = raw.get("allow_loopback_http", False)
    if not isinstance(allow_loopback, bool):
        raise SpecError("allow_loopback_http must be a boolean")
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list):
        raise SpecError("checks must be a list")
    if not MIN_CHECKS <= len(checks_raw) <= MAX_CHECKS:
        raise SpecError(f"checks must contain {MIN_CHECKS} to {MAX_CHECKS} items")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in checks_raw:
        checks.append(_normalize_check(item, seen))
    spec = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "workspace_root": workspace,
        "freshness_seconds": freshness,
        "allow_loopback_http": allow_loopback,
        "checks": checks,
    }
    return spec


def _normalize_check(item: Any, seen: set[str]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise SpecError("each check must be an object")
    kind = item.get("kind")
    if kind not in CHECK_KINDS:
        raise SpecError("check kind must be file, test, or website")
    check_id = require_id(item.get("id"), "check id")
    if check_id in seen:
        raise SpecError(f"duplicate check id {check_id}")
    seen.add(check_id)
    if kind == "file":
        return _file_check(item, check_id)
    if kind == "test":
        return _test_check(item, check_id)
    return _website_check(item, check_id)


def _file_check(item: dict[str, Any], check_id: str) -> dict[str, Any]:
    allowed = {"id", "kind", "path", "contains", "json_keys"}
    _reject_unknown(item, allowed, check_id)
    path = _bounded_str(item.get("path"), f"{check_id}.path", MAX_PATH)
    assert path is not None
    if path.startswith("/") or "\\" in path or path.split("/")[0] in {"", ".", ".."}:
        raise SpecError(f"{check_id}.path must be workspace-relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SpecError(f"{check_id}.path must not traverse")
    has_contains = "contains" in item
    has_keys = "json_keys" in item
    if has_contains == has_keys:
        raise SpecError(f"{check_id} needs exactly one of contains or json_keys")
    out: dict[str, Any] = {"id": check_id, "kind": "file", "path": path}
    if has_contains:
        text = _bounded_str(item.get("contains"), f"{check_id}.contains", MAX_EXPECTATION)
        out["contains"] = text
    else:
        keys = item.get("json_keys")
        if not isinstance(keys, list) or not keys or len(keys) > MAX_JSON_KEYS:
            raise SpecError(f"{check_id}.json_keys must be a short non-empty list")
        clean: list[str] = []
        for key in keys:
            if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN:
                raise SpecError(f"{check_id}.json_keys entries must be short strings")
            if key in clean:
                raise SpecError(f"{check_id}.json_keys has a duplicate")
            clean.append(key)
        out["json_keys"] = clean
    return out


def _test_check(item: dict[str, Any], check_id: str) -> dict[str, Any]:
    allowed = {"id", "kind", "command", "workdir", "expected_exit", "contains", "timeout"}
    _reject_unknown(item, allowed, check_id)
    command = _bounded_str(item.get("command"), f"{check_id}.command", MAX_COMMAND)
    workdir = _bounded_str(item.get("workdir"), f"{check_id}.workdir", MAX_PATH)
    assert command is not None and workdir is not None
    if not workdir.startswith("/"):
        raise SpecError(f"{check_id}.workdir must be absolute")
    expected = item.get("expected_exit", 0)
    expected = _int(expected, f"{check_id}.expected_exit", -255, 255)
    timeout = item.get("timeout", 30)
    timeout = _int(timeout, f"{check_id}.timeout", MIN_TIMEOUT, MAX_TIMEOUT)
    out: dict[str, Any] = {
        "id": check_id,
        "kind": "test",
        "command": command,
        "workdir": workdir,
        "expected_exit": expected,
        "timeout": timeout,
    }
    if "contains" in item:
        out["contains"] = _bounded_str(item.get("contains"), f"{check_id}.contains", MAX_EXPECTATION)
    return out


def _website_check(item: dict[str, Any], check_id: str) -> dict[str, Any]:
    allowed = {"id", "kind", "url", "expected_status", "contains"}
    _reject_unknown(item, allowed, check_id)
    url = _bounded_str(item.get("url"), f"{check_id}.url", MAX_URL)
    assert url is not None
    status = _int(item.get("expected_status"), f"{check_id}.expected_status", 100, 599)
    contains = _bounded_str(item.get("contains"), f"{check_id}.contains", MAX_EXPECTATION)
    return {
        "id": check_id,
        "kind": "website",
        "url": url,
        "expected_status": status,
        "contains": contains,
    }


def _reject_unknown(item: dict[str, Any], allowed: set[str], check_id: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise SpecError(f"{check_id} has unknown fields: {', '.join(unknown)}")


def aggregate_state(states: list[str]) -> str:
    """failed wins; otherwise any gap is unverified; verified only if every required check passed."""
    if not states:
        return "unverified"
    if any(state == "failed" for state in states):
        return "failed"
    if all(state == "passed" for state in states):
        return "verified"
    return "unverified"


def host_context(task_id: Any, session_id: Any) -> tuple[str | None, str | None]:
    """Host identity from dispatcher kwargs only. Empty values are missing, not a global fallback."""
    host_task = task_id if isinstance(task_id, str) and task_id.strip() else None
    host_session = session_id if isinstance(session_id, str) and session_id.strip() else None
    return host_task, host_session


def host_context_complete(host_task: str | None, host_session: str | None) -> bool:
    return bool(host_task and host_session)
