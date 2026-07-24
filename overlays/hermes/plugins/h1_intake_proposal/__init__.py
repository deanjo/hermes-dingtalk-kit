"""H1 intake proposal plugin — standalone, opt-in via ``plugins.enabled``.

Registers one narrow tool (``h1_intake_propose``) implementing the H1
interaction-layer proposal seam (design H1_INTERACTION_LAYER_DESIGN_20260723
§2.3): the model supplies the proposal content; code validates and slots
it; the pending installs only after the turn's non-receipt final reply is
provably delivered (C1).  Kept ``kind: standalone`` deliberately: enabling
it on a live gateway is an explicit config rollout step.
"""

from __future__ import annotations

from .tools import (
    H1_INTAKE_PROPOSE_SCHEMA,
    _handle_propose,
    capture_dispatch_context,
)


def register(ctx) -> None:
    """Register the proposal tool. Called by the plugin loader."""
    ctx.register_hook("pre_gateway_dispatch", capture_dispatch_context)
    ctx.register_tool(
        name="h1_intake_propose",
        toolset="h1_intake_proposal",
        schema=H1_INTAKE_PROPOSE_SCHEMA,
        handler=_handle_propose,
        emoji="📌",
    )
