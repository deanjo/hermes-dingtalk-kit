"""``h1_intake_propose`` — the H1 model's structured intake-proposal signal.

Hard rules in code (design §2.3 / C1 / I2 / I5):

* Identity and authority come from the platform event captured by the
  public ``pre_gateway_dispatch`` hook — the ``event.source`` OBJECT and
  the real inbound msgId — never from model arguments.  A missing
  dispatch context, or a blank/synthetic msgId, fails closed.
* The tool ONLY validates + slots (zero Kanban writes, zero pending
  installs): Core's ``build_intake_proposal`` performs identity
  normalization, the drift guard, per-kind target validation and the
  pending prebuild; ``original_text`` is the proposal-triggering user
  message verbatim (I2) — never the model's proposal text.
* The pending installs later, in the post-delivery callback, once the
  turn's non-receipt final reply is provably delivered (C1), and is
  registered for quote authentication atomically in the same callback.
  A same-message repeat call is idempotent (same operation_id); a second
  distinct proposal in one turn is rejected.
"""

from __future__ import annotations

import contextvars
import importlib.util
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


H1_INTAKE_PROPOSE_SCHEMA = {
    "name": "h1_intake_propose",
    "description": (
        "Propose a task-intake action to the user (create a task / bind an "
        "existing task / create a project). This ONLY records a validated "
        "proposal — code installs the confirmation pending after your reply "
        "is actually delivered, and nothing is ever written to Kanban "
        "without the user's next-message confirmation. The proposal text "
        "you speak in the same reply is what the user confirms against. "
        "Call at most once per reply; never call when a confirmation is "
        "already pending (the tool rejects and you should instead remind "
        "the user of the outstanding confirmation in your own words)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["create_task", "bind_existing", "new_project"],
            },
            "board_slug": {"type": "string"},
            "board_name": {"type": "string"},
            "task_title": {"type": "string"},
            "body": {"type": "string"},
            "candidates": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "object"},
            },
            "proposal_text": {"type": "string", "maxLength": 1000},
        },
        "required": ["kind", "proposal_text"],
    },
}


@dataclass(frozen=True)
class _DispatchScope:
    """Per-message gateway context propagated into the tool worker thread."""

    source: Any = None
    message_id: str = ""
    text: str = ""
    gateway: Any = None
    session_store: Any = None


_dispatch_scope: contextvars.ContextVar[Optional[_DispatchScope]] = (
    contextvars.ContextVar("h1_intake_proposal_dispatch_scope", default=None)
)


def capture_dispatch_context(event: Any, gateway: Any, session_store: Any = None, **kwargs: Any) -> None:
    """Capture the platform event's source OBJECT and real inbound msgId.

    I5: ``_normalized_identity_source`` and ``task_state_key`` need the
    complete source object — capturing only fields (product_confirmation
    style) is not enough here.  The ContextVar rides Hermes' propagation
    into tool-worker threads.
    """
    source = getattr(event, "source", None)
    _dispatch_scope.set(
        _DispatchScope(
            source=source,
            message_id=str(
                getattr(event, "message_id", "")
                or getattr(source, "message_id", "")
                or ""
            ),
            text=str(getattr(event, "text", "") or ""),
            gateway=gateway,
            session_store=session_store,
        )
    )


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _task_binding_module():
    """Resolve the dingtalk kit's task_binding helpers.

    Every mutable structure they touch lives on the ADAPTER instance
    (slot dict, delivery records, outcomes, quote registry), so any loaded
    instance of the module drives the same coherent behavior.  Production
    has it as a hermes_plugins submodule (the adapter imported it first);
    tests register their own instance under a plain alias.
    """
    for name in (
        "hermes_plugins.platforms__dingtalk.task_binding",
        "task_binding",
        "dingtalk_task_binding",
        "dingtalk_task_binding_for_intake_uut",
    ):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "slot_h1_proposal"):
            return module
    try:
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "platforms"
            / "dingtalk"
            / "task_binding.py"
        )
        spec = importlib.util.spec_from_file_location("dingtalk_task_binding", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("dingtalk_task_binding", module)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - resolve failure fails closed below
        logger.warning("h1_intake_propose: task_binding module unavailable", exc_info=True)
        return None


def _handle_propose(args: dict, **_kwargs: Any) -> str:
    """Validate + slot one intake proposal (C1 first half)."""
    scope = _dispatch_scope.get()
    if scope is None or scope.source is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})
    tb = _task_binding_module()
    if tb is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})
    adapter = None
    gateway = scope.gateway
    if gateway is not None:
        try:
            adapter = gateway._adapter_for_source(scope.source)
        except Exception:  # noqa: BLE001 - no adapter => cannot slot/callback
            adapter = None
    if adapter is None or scope.session_store is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})

    # I2: original_text is the proposal-triggering user message verbatim.
    # The adapter's dispatch scope carries the pre-mention-meta text (the
    # user's own words); the hook's event text is the fallback.
    original_text = scope.text
    try:
        adapter_scope = tb.h1_dispatch_scope()
    except Exception:  # noqa: BLE001 - best-effort refinement only
        adapter_scope = None
    if (
        adapter_scope
        and adapter_scope.get("message_id") == scope.message_id
        and adapter_scope.get("text")
    ):
        original_text = adapter_scope["text"]

    try:
        from gateway.task_intake import build_intake_proposal
    except Exception:  # noqa: BLE001 - old Core without the seam
        return _json({"ok": False, "reason": "proposal_seam_unavailable"})

    result = build_intake_proposal(
        scope.session_store,
        scope.source,
        kind=args.get("kind"),
        request_id=scope.message_id,
        original_text=original_text,
        proposal_text=args.get("proposal_text"),
        board_slug=args.get("board_slug"),
        board_name=args.get("board_name"),
        task_title=args.get("task_title"),
        body=args.get("body"),
        candidates=args.get("candidates"),
    )
    if not result.get("ok"):
        return _json(result)

    pending = result["pending"]
    source = result["source"]
    try:
        session_key = tb.session_key_for_h1(adapter, source)
    except Exception:  # noqa: BLE001 - key derivation must not install
        return _json({"ok": False, "reason": "session_key_unavailable"})
    generation = tb.current_h1_generation(adapter, session_key)

    slot_status = tb.slot_h1_proposal(
        adapter,
        session_key,
        pending=pending,
        source=source,
        generation=generation,
    )
    if slot_status == "already_slotted":
        # Same message redelivered (M5): idempotent, no duplicate callback.
        return _json(
            {
                "ok": True,
                "operation_id": result["operation_id"],
                "note": "提案已记录，将于本回复送达后生效",
            }
        )
    if slot_status is not None:
        return _json({"ok": False, "reason": "proposal_in_flight"})

    adapter.register_post_delivery_callback(
        session_key,
        lambda: tb.install_h1_proposal_after_delivery(adapter, session_key),
        generation=generation,
    )
    return _json(
        {
            "ok": True,
            "operation_id": result["operation_id"],
            "note": "提案将于本回复送达后生效",
        }
    )
