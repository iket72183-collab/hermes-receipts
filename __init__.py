"""Hermes Receipts — explicit check evidence, not a completion certificate."""

from __future__ import annotations

from . import observe, tools

TOOLSET = "hermes-receipts"


def register(ctx) -> None:
    """Register tools, hooks, a slash command, and an optional CLI. No I/O."""
    for name, handler, description, parameters, required in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema={
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": parameters, "required": required},
            },
            handler=handler,
            description=description,
        )
    ctx.register_hook("pre_tool_call", observe.on_pre_tool_call)
    ctx.register_hook("post_tool_call", observe.on_post_tool_call)
    ctx.register_command(
        "receipts",
        tools.slash_receipts,
        description="Show persisted receipts for a caller task id",
        args_hint="<task-id> [attempt-id]",
    )
    ctx.register_cli_command(
        "receipts",
        help="Read and seal Hermes Receipts from an isolated local spec",
        setup_fn=tools.setup_cli,
        handler_fn=tools.cli_main,
        description="Deterministic local receipts. Terminal evidence stays unverified without host identity.",
    )


def _obj(description: str) -> dict:
    return {"type": "object", "description": description}


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}


_TOOLS = (
    (
        "receipts_begin",
        tools.receipts_begin,
        "Begin a receipt attempt from an explicit file, test, and website spec. Creates a new attempt id and criteria digest. Does not run commands.",
        {
            "spec": _obj(
                "schema_version 1, task_id, absolute workspace_root, and 1-20 file/test/website checks. "
                "allow_loopback_http opts this spec into loopback HTTP for a local demo."
            ),
        },
        ["spec"],
    ),
    (
        "receipts_check",
        tools.receipts_check,
        "Evaluate file and website checks now, seal matching observed terminal evidence, and store an immutable receipt. Model-supplied exit codes are rejected.",
        {"attempt_id": _str("Attempt id returned by receipts_begin.")},
        ["attempt_id"],
    ),
    (
        "receipts_get",
        tools.receipts_get,
        "Read persisted receipts for a caller-visible task id. Does not certify anything new.",
        {
            "task_id": _str("Caller-visible task id from the spec."),
            "attempt_id": _str("Optional attempt id."),
        },
        ["task_id"],
    ),
    (
        "receipts_export",
        tools.receipts_export,
        "Write a JSON or HTML export into plugin-owned storage. Public export omits raw command output. No caller-chosen path.",
        {
            "attempt_id": _str("Sealed attempt id."),
            "format": {"type": "string", "enum": ["json", "html"], "description": "Export format."},
            "public": _bool("Default true. Omit raw command output and absolute paths."),
        },
        ["attempt_id"],
    ),
)
