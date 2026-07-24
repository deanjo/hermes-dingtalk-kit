"""H1 V2 thin write-gate plugin — standalone, opt-in via ``plugins.enabled``.

Registers ``h1_declare_proposal`` plus the four write tools
(``create_task`` / ``bind_task`` / ``switch_task`` / ``unbind_task``)
implementing the H1 V2 thin-gate contract (H1_V2_BARE_AGENT_DESIGN §3.2):
the model declares; code checks structural facts; business arguments come
only from the declared record.  Identity and the inbound msgId come from
the platform event, never from model arguments.
"""

from __future__ import annotations

from .tools import (
    H1_DECLARE_PROPOSAL_SCHEMA,
    H1_WRITE_TOOL_SCHEMAS,
    _check_h1_task_write_mode,
    _handle_bind_task,
    _handle_create_task,
    _handle_declare,
    _handle_switch_task,
    _handle_unbind_task,
    capture_dispatch_context,
)

_TOOLS = (
    ("create_task", H1_WRITE_TOOL_SCHEMAS["create_task"], _handle_create_task, "📝"),
    ("bind_task", H1_WRITE_TOOL_SCHEMAS["bind_task"], _handle_bind_task, "🔗"),
    ("switch_task", H1_WRITE_TOOL_SCHEMAS["switch_task"], _handle_switch_task, "🔀"),
    ("unbind_task", H1_WRITE_TOOL_SCHEMAS["unbind_task"], _handle_unbind_task, "↩️"),
)


def register(ctx) -> None:
    """Register the declare + write tools. Called by the plugin loader."""
    ctx.register_hook("pre_gateway_dispatch", capture_dispatch_context)
    ctx.register_tool(
        name="h1_declare_proposal",
        toolset="h1_task_write",
        schema=H1_DECLARE_PROPOSAL_SCHEMA,
        handler=_handle_declare,
        check_fn=_check_h1_task_write_mode,
        emoji="📌",
    )
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="h1_task_write",
            schema=schema,
            handler=handler,
            check_fn=_check_h1_task_write_mode,
            emoji=emoji,
        )
