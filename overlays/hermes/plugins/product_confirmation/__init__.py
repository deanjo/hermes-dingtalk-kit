"""Product-confirmation plugin — standalone, opt-in via ``plugins.enabled``.

Registers five narrow tools implementing the product-confirmation contract
(see ``plugins/product_confirmation/store.py`` for the state machine and
``tools.py`` for the identity / delivery rules). Kept ``kind: standalone``
deliberately: enabling it on a live gateway is an explicit config rollout
step, never an implicit side effect of an image upgrade.
"""

from __future__ import annotations

from .tools import (
    PRODUCT_CONFIRM_ADVANCE_SCHEMA,
    PRODUCT_CONFIRM_DECIDE_SCHEMA,
    PRODUCT_CONFIRM_DRAFT_SCHEMA,
    PRODUCT_CONFIRM_REQUEST_SCHEMA,
    PRODUCT_CONFIRM_STATUS_SCHEMA,
    _handle_advance,
    _handle_decide,
    _handle_draft,
    _handle_request,
    _handle_status,
    capture_dispatch_context,
)

_TOOLS = (
    ("product_confirm_draft",   PRODUCT_CONFIRM_DRAFT_SCHEMA,   _handle_draft,   "📝"),
    ("product_confirm_request", PRODUCT_CONFIRM_REQUEST_SCHEMA, _handle_request, "📨"),
    ("product_confirm_decide",  PRODUCT_CONFIRM_DECIDE_SCHEMA,  _handle_decide,  "✅"),
    ("product_confirm_advance", PRODUCT_CONFIRM_ADVANCE_SCHEMA, _handle_advance, "🏗️"),
    ("product_confirm_status",  PRODUCT_CONFIRM_STATUS_SCHEMA,  _handle_status,  "🔎"),
)


def register(ctx) -> None:
    """Register all product-confirmation tools. Called by the plugin loader."""
    ctx.register_hook("pre_gateway_dispatch", capture_dispatch_context)
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="product_confirmation",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )
