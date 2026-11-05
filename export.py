"""JSON and HTML exports from an immutable receipt snapshot.

Exports land only in plugin-owned storage under generated filenames.
Public exports omit raw command output and absolute paths.
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

from .checks import sanitize_url
from .schema import valid_id

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|aws_access_key|BEGIN [A-Z ]*PRIVATE KEY)[=: ]+\S+"
)


def public_view(receipt: dict[str, Any]) -> dict[str, Any]:
    """Redacted view. Redaction is not a guarantee; public demos must use synthetic data."""
    view = json.loads(json.dumps(receipt))
    view.pop("host_task_id", None)
    view.pop("host_session_id", None)
    spec = view.get("spec")
    if isinstance(spec, dict):
        if "workspace_root" in spec:
            spec["workspace_root"] = "<redacted-path>"
        for check in spec.get("checks") or []:
            if isinstance(check, dict) and isinstance(check.get("url"), str):
                check["url"] = sanitize_url(check["url"])
            if isinstance(check, dict) and "workdir" in check:
                check["workdir"] = "<redacted-path>"
            if isinstance(check, dict) and "command" in check:
                check["command"] = "<redacted-command>"
    for check in view.get("checks") or []:
        if not isinstance(check, dict):
            continue
        evidence = check.get("evidence")
        if isinstance(evidence, dict):
            if isinstance(evidence.get("url"), str):
                evidence["url"] = sanitize_url(evidence["url"])
            evidence.pop("output", None)
            evidence.pop("command", None)
            for key in ("workdir", "path", "full_output_path"):
                if key in evidence and isinstance(evidence[key], str) and evidence[key].startswith("/"):
                    evidence[key] = "<redacted-path>"
        reason = check.get("reason")
        if isinstance(reason, str):
            check["reason"] = _SECRET_RE.sub("<redacted>", reason)
    view["public_export"] = True
    view["raw_command_output_omitted"] = True
    def redact(value):
        if isinstance(value, str):
            return _SECRET_RE.sub("<redacted>", value)
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        return value
    return redact(view)


def render_json(receipt: dict[str, Any], *, public: bool = True) -> str:
    payload = public_view(receipt) if public else receipt
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_html(receipt: dict[str, Any], *, public: bool = True) -> str:
    data = public_view(receipt) if public else receipt
    rows = []
    cards = []
    criteria = {c.get("id"): c for c in (data.get("spec") or {}).get("checks", []) if isinstance(c, dict)}
    for check in data.get("checks") or []:
        if not isinstance(check, dict):
            continue
        evidence = check.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        bits = []
        for key in sorted(evidence):
            value = evidence[key]
            if key == "output":
                continue
            bits.append(f"{_esc(str(key))}={_esc(_short(value))}")
        rows.append(
            "<tr>"
            f"<td>{_esc(check.get('id'))}</td>"
            f"<td>{_esc(check.get('kind'))}</td>"
            f"<td>{_esc(check.get('state'))}</td>"
            f"<td>{_esc(check.get('reason'))}</td>"
            f"<td>{_esc(check.get('provenance'))}</td>"
            f"<td>{' '.join(bits)}</td>"
            "</tr>"
        )
        criterion = criteria.get(check.get("id"), {})
        expectation = criterion.get("contains") or ("Required JSON keys: " + ", ".join(criterion.get("json_keys", [])))
        if check.get("kind") == "test":
            expectation = f"Exit {criterion.get('expected_exit', 0)}" + (f" · {expectation}" if criterion.get("contains") else "")
        if check.get("kind") == "website":
            expectation = f"HTTP {criterion.get('expected_status')} · {expectation}"
        state = check.get("state", "unverified")
        cards.append(f'<section class="check-card"><div class="kind">{_esc(check.get("kind"))}</div><h2>{_esc(check.get("id"))}</h2><strong class="{_esc(state)}">{_esc(state).upper()}</strong><p>{_esc(check.get("reason"))}</p><p class="expectation">Expected: {_esc(expectation)}</p></section>')
    title = f"Receipt {_esc(data.get('task_id'))} {_esc(data.get('attempt_id'))}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ margin: 2rem auto; padding: 0 2rem; max-width: 1300px; font: 16px/1.45 ui-sans-serif, system-ui, sans-serif; background: #10151d; color: #eceff4; }}
h1 {{ margin: 0.3rem 0; font-size: 2.5rem; font-weight: 650; letter-spacing: -0.04em; }}
h2 {{ margin: .4rem 0; font-size: 1.25rem; }}
.brand,.kind {{ text-transform: uppercase; font-size: .8rem; letter-spacing: .12em; color: #9baec3; }}
.state {{ font-size: 2.8rem; font-weight: 750; text-transform: uppercase; margin: .5rem 0; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap: 1rem; margin:1.5rem 0; }}
.check-card {{ padding:1.25rem; border:1px solid #354154; border-radius:12px; background:#17212e; }}
.expectation {{ color: #aebfd1; font-size: .9rem; }}
details {{ margin: 1.25rem 0; }}
summary {{ cursor:pointer; padding:.7rem 0; color:#c4d6eb; }}
p, li {{ max-width: 46rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #2a2f3a; padding: 0.4rem 0.55rem; text-align: left; vertical-align: top; overflow-wrap:anywhere; }}
li {{ overflow-wrap:anywhere; }}
.failed {{ color: #ff8b8b; }}
.verified {{ color: #9be7b5; }}
.unverified {{ color: #e6c07b; }}
</style>
</head>
<body>
<div class="brand">BDT Talent Group · Evidence-backed task checks</div>
<h1>Hermes Receipts</h1>
<div class="state {_esc(data.get('state'))}">{_esc(data.get('state'))}</div>
<p>As-of snapshot of explicit checks. Not a lie detector, security sandbox, or signed attestation.</p>
<div class="cards">{''.join(cards)}</div>
<details><summary>Inspect timestamps, criteria digest and check evidence</summary>
<ul>
<li>Task {_esc(data.get('task_id'))}</li>
<li>Attempt {_esc(data.get('attempt_id'))}</li>
<li>State <span class="{_esc(data.get('state'))}">{_esc(data.get('state'))}</span></li>
<li>Digest {_esc(data.get('criteria_digest'))}</li>
<li>Started {_esc(data.get('started_at'))}</li>
<li>Checked {_esc(data.get('checked_at'))}</li>
</ul>
<table>
<thead><tr><th>Check</th><th>Kind</th><th>State</th><th>Reason</th><th>Provenance</th><th>Evidence</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</details>
<p>Public export omits raw command output. Observed native terminal execution is not a human approval. Local plugin data is not telemetry; delete the profile plugin-data/hermes-receipts directory to remove it.</p>
</body>
</html>
"""


def write_export(receipt: dict[str, Any], export_dir: Path, *, fmt: str, public: bool = True) -> Path:
    attempt_id = receipt.get("attempt_id")
    if not valid_id(attempt_id):
        raise ValueError("refusing to export an unsafe attempt id")
    if fmt not in {"json", "html"}:
        raise ValueError("format must be json or html")
    export_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(export_dir, 0o700)
    filename = f"receipt-{attempt_id}.{fmt}"
    if not valid_id(attempt_id) or "/" in filename or ".." in filename:
        raise ValueError("refusing unsafe export filename")
    target = export_dir / filename
    if target.parent.resolve() != export_dir.resolve():
        raise ValueError("export path escaped plugin storage")
    text = render_json(receipt, public=public) if fmt == "json" else render_html(receipt, public=public)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    return target


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if len(text) > 180:
        return text[:180] + "…"
    return text
