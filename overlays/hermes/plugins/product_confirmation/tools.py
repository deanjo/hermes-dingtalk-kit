"""Agent-callable tools for the product-confirmation state machine.

Eight narrow tools — two confirmation gates plus status — over
:mod:`plugins.product_confirmation.store`. Hard rules live in code, not in
prompts:

* The decision actor and live gateway are captured by Hermes' public
  ``pre_gateway_dispatch`` hook. The actor is exactly
  ``event.source.user_id_alt`` from the real inbound event, never a model
  argument or private environment bridge. Missing identity fails closed.
* The product owner's platform id comes only from runtime config
  (``plugins.entries.product_confirmation.product_owner_staff_id``) — it is
  never hardcoded and never echoed back in tool results.
* ``product_confirm_request`` composes the confirmation message itself (all
  contract-required fields) and delivers it to the *current* chat via the
  live platform adapter with a structured @-mention of the owner. This is a
  domain-scoped send, not a general messaging capability — ``send_message``
  intentionally stays non-agent-callable (see ``tools/send_message_tool.py``).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import store as pc_store

logger = logging.getLogger(__name__)

_PLUGIN_ID = "product_confirmation"
_SEND_TIMEOUT_SECONDS = 30.0

_store_lock = threading.Lock()
_store: Optional[pc_store.ProductConfirmationStore] = None


@dataclass(frozen=True)
class _DispatchContext:
    """Per-message public gateway context propagated into tool workers."""

    platform: str = ""
    chat_id: str = ""
    actor_staff_id: str = ""
    message_id: str = ""
    gateway: Any = None
    loop: Any = None


_dispatch_context: contextvars.ContextVar[Optional[_DispatchContext]] = (
    contextvars.ContextVar("product_confirmation_dispatch_context", default=None)
)


def capture_dispatch_context(event: Any, gateway: Any, **kwargs: Any) -> None:
    """Capture only fields supplied by the public ``pre_gateway_dispatch`` hook.

    Hermes propagates the turn's ContextVars into tool-worker threads. Keeping
    the gateway and sender here removes the old dependency on private gateway
    globals and session-environment bridges.
    """
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_name = platform.value if hasattr(platform, "value") else str(platform or "")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    _dispatch_context.set(
        _DispatchContext(
            platform=platform_name,
            chat_id=str(getattr(source, "chat_id", "") or ""),
            actor_staff_id=str(getattr(source, "user_id_alt", "") or ""),
            message_id=str(
                getattr(event, "message_id", "")
                or getattr(source, "message_id", "")
                or ""
            ),
            gateway=gateway,
            loop=loop,
        )
    )


def _get_store() -> pc_store.ProductConfirmationStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = pc_store.ProductConfirmationStore()
        return _store


def _plugin_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return {}
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    return entries.get(_PLUGIN_ID) or {}


def _resolve_owner() -> Tuple[str, str]:
    """Return owner identity from the plugin's normal runtime config."""
    entry = _plugin_config()
    staff_id = str(entry.get("product_owner_staff_id") or "").strip()
    name = str(entry.get("product_owner_name") or "").strip() or "产品负责人"
    return staff_id, name


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _current_conversation() -> str:
    context = _dispatch_context.get()
    if context and context.platform and context.chat_id:
        return f"{context.platform}:{context.chat_id}"
    return ""


def _scoped_task_key(logical_task_id: str, source_conversation: str) -> str:
    """Opaque DB key for the public ``(conversation, task_id)`` namespace."""
    material = f"{source_conversation}\0{logical_task_id}"
    return "pc:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _resolve_scoped_record(
    store: pc_store.ProductConfirmationStore,
    logical_task_id: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve only in the current conversation and explain cross-chat misses."""
    current = _current_conversation()
    internal_task_id = store.resolve_task_key(logical_task_id, current)
    if internal_task_id:
        return internal_task_id, store.get(internal_task_id), None
    if store.logical_task_exists(logical_task_id):
        return None, None, {
            "ok": False,
            "reason": "WRONG_CONVERSATION",
            "error": "this task id exists, but not in the current origin"
                     f" conversation ({current or 'unknown'}); state unchanged",
        }
    return None, None, {
        "ok": False,
        "reason": "UNKNOWN_TASK",
        "error": f"unknown task {logical_task_id!r} in the current conversation",
    }


def _public_result(result: Dict[str, Any], logical_task_id: str) -> Dict[str, Any]:
    public = dict(result)
    if "task_id" in public:
        public["task_id"] = logical_task_id
    return public


def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(record)
    public["task_id"] = record.get("logical_task_id") or record.get("task_id")
    public.pop("logical_task_id", None)
    return public


# -- draft ---------------------------------------------------------------------

PRODUCT_CONFIRM_DRAFT_SCHEMA = {
    "name": "product_confirm_draft",
    "description": (
        "Register a product proposal version for a task (state PRODUCT_DRAFT). "
        "Call this when the product proposal for a requirement is ready, and "
        "again with a bumped proposal_version after a NEEDS_REVISION decision. "
        "Each (task_id, proposal_version) is immutable and unique."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Stable requirement id, e.g. 'REQ-20260714-01'"},
            "proposal_version": {"type": "string", "description": "Proposal version, e.g. 'v1'"},
            "proposal_text": {"type": "string", "description": "Full proposal text; only its digest is stored"},
            "proposal_digest": {"type": "string", "description": "Precomputed digest (alternative to proposal_text)"},
        },
        "required": ["task_id", "proposal_version"],
    },
}


def _handle_draft(args: dict, **kw) -> str:
    task_id = str(args.get("task_id") or "").strip()
    version = str(args.get("proposal_version") or "").strip()
    if not task_id or not version:
        return _json({"ok": False,
                      "error": "task_id and proposal_version are required"})
    text = args.get("proposal_text") or ""
    digest = str(args.get("proposal_digest") or "").strip() or (
        _digest(text) if text else ""
    )
    if not digest:
        return _json({"error": "provide proposal_text or proposal_digest so the"
                               " version can be uniquely identified"})
    source_conversation = _current_conversation()
    internal_task_id = _scoped_task_key(task_id, source_conversation)
    _, owner_name = _resolve_owner()
    result = _get_store().create_draft(
        internal_task_id, version, digest,
        source_conversation=source_conversation,
        product_owner=owner_name,
        logical_task_id=task_id,
    )
    return _json(_public_result(result, task_id))


# -- request -------------------------------------------------------------------

PRODUCT_CONFIRM_REQUEST_SCHEMA = {
    "name": "product_confirm_request",
    "description": (
        "Send the product-confirmation request for a drafted proposal to the "
        "product owner in the current chat (structured @-mention) and move the "
        "task to WAITING_PRODUCT_CONFIRMATION. Requires the task to be in "
        "PRODUCT_DRAFT at exactly this proposal_version. The message content "
        "is composed by this tool from the fields below."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "proposal_version": {"type": "string"},
            "title": {"type": "string", "description": "Requirement title"},
            "problem": {"type": "string", "description": "User problem being solved"},
            "target_users": {"type": "string", "description": "Who this serves"},
            "expected_behavior": {"type": "string", "description": "Expected behavior after the change"},
            "scope_in": {"type": "array", "items": {"type": "string"}, "description": "In scope"},
            "scope_out": {"type": "array", "items": {"type": "string"}, "description": "Out of scope"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id", "proposal_version", "title", "problem",
                     "target_users", "expected_behavior", "scope_in",
                     "scope_out", "acceptance_criteria"],
    },
}


def _compose_confirmation_message(
    args: dict, record: Dict[str, Any], owner_name: str, confirmation_code: str,
) -> str:
    def _bullets(items: Any) -> str:
        return "\n".join(f"  - {str(i)}" for i in (items or []))

    return (
        f"### 产品方案确认请求\n"
        f"@{owner_name} 请确认以下产品方案。\n\n"
        f"- **需求**: {args['title']}\n"
        f"- **task_id**: `{record['logical_task_id']}`\n"
        f"- **方案版本**: `{record['proposal_version']}`"
        f"（digest `{record['proposal_digest']}`）\n"
        f"- **用户问题**: {args['problem']}\n"
        f"- **目标用户**: {args['target_users']}\n"
        f"- **期望行为**: {args['expected_behavior']}\n"
        f"- **范围内**:\n{_bullets(args.get('scope_in'))}\n"
        f"- **范围外**:\n{_bullets(args.get('scope_out'))}\n"
        f"- **验收标准**:\n{_bullets(args.get('acceptance_criteria'))}\n\n"
        f"请 {owner_name} **在本群 @我 回复**其中之一，"
        f"并带上确认码 `{confirmation_code}`：\n"
        f"1. **同意进入技术设计 {confirmation_code}**\n"
        f"2. **需要修改 {confirmation_code}**（请说明修改点）\n\n"
        f"（不 @ 我或缺少确认码的回复无法被系统受理）"
    )


def _deliver_to_current_chat(content: str, at_staff_id: str) -> Dict[str, Any]:
    """Send *content* to the current session's chat via the live adapter.

    Runs from a sync tool thread; the coroutine is scheduled on the gateway's
    own event loop (the adapter's HTTP client is bound to it). Fails closed
    when any link is missing — no delivery, no state change.
    """
    context = _dispatch_context.get()
    platform_name = context.platform if context else ""
    chat_id = context.chat_id if context else ""
    if platform_name != "dingtalk" or not chat_id:
        return {"error": "confirmation delivery requires an active DingTalk"
                         f" session (platform={platform_name!r})",
                "delivery_outcome": "not_sent"}
    runner = context.gateway if context else None
    if runner is None:
        return {"error": "gateway is not running in this process; cannot"
                         " deliver the confirmation request",
                "delivery_outcome": "not_sent"}
    adapter = None
    try:
        for platform, candidate in getattr(runner, "adapters", {}).items():
            key = platform.value if hasattr(platform, "value") else str(platform)
            if key == platform_name:
                adapter = candidate
                break
    except Exception:
        adapter = None
    if adapter is None:
        return {"error": "no live DingTalk adapter is connected",
                "delivery_outcome": "not_sent"}
    loop = context.loop if context else None
    if loop is None:
        return {"error": "gateway event loop unavailable; cannot deliver",
                "delivery_outcome": "not_sent"}
    future = None
    try:
        future = asyncio.run_coroutine_threadsafe(
            adapter.send(
                chat_id,
                content,
                metadata={
                    "at_user_ids": [at_staff_id],
                    # H1 治理第 5 项：这是合法业务投递，出站闸门必须放行
                    # （误杀即打断场景 2 的需求确认流程）。
                    "delivery_class": "business_confirm",
                },
            ),
            loop,
        )
        result = future.result(timeout=_SEND_TIMEOUT_SECONDS)
    except Exception as exc:
        if future is not None:
            future.cancel()
        return {
            "error": f"confirmation delivery failed: {exc}",
            # Once the coroutine was submitted, an exception or timeout cannot
            # prove whether DingTalk accepted the request.  Keeping the durable
            # claim prevents an unsafe automatic duplicate.
            "delivery_outcome": "unknown" if future is not None else "not_sent",
        }
    raw_response = getattr(result, "raw_response", None)
    adapter_outcome = (
        raw_response.get("delivery_outcome")
        if isinstance(raw_response, dict)
        else None
    )
    if adapter_outcome not in {"delivered", "rejected", "unknown"}:
        # Missing/invalid adapter metadata is itself uncertain.  Never infer
        # retry safety from the human-readable error string.
        adapter_outcome = "unknown"
    if not getattr(result, "success", False):
        error = str(getattr(result, "error", "unknown send error"))
        return {
            "error": f"confirmation delivery failed: {error}",
            "delivery_outcome": (
                adapter_outcome
                if adapter_outcome in {"rejected", "unknown"}
                else "unknown"
            ),
        }
    if adapter_outcome != "delivered":
        return {
            "error": "confirmation adapter reported success without a"
                     " delivered outcome",
            "delivery_outcome": "unknown",
        }
    return {
        "message_id": getattr(result, "message_id", "") or "",
        "delivery_outcome": "delivered",
    }


def _handle_request(args: dict, **kw) -> str:
    logical_task_id = str(args.get("task_id") or "").strip()
    version = str(args.get("proposal_version") or "").strip()
    store = _get_store()
    task_id, record, resolution_error = _resolve_scoped_record(
        store, logical_task_id
    )
    if resolution_error:
        resolution_error["error"] += "; call product_confirm_draft first"
        return _json(resolution_error)
    assert task_id is not None and record is not None
    if record["status"] != pc_store.PRODUCT_DRAFT:
        return _json({"error": f"task is in {record['status']}; a confirmation"
                               " request requires PRODUCT_DRAFT",
                      "status": record["status"]})
    if record["proposal_version"] != version:
        return _json({"error": f"current proposal version is"
                               f" {record['proposal_version']!r}, not"
                               f" {version!r}"})
    owner_staff_id, owner_name = _resolve_owner()
    if not owner_staff_id:
        return _json({"error": "product owner staff id is not configured; set"
                               " plugins.entries.product_confirmation."
                               "product_owner_staff_id. Refusing to"
                               " send an untargeted confirmation."})
    # Persist the claim before any external send.  The raw one-time code lives
    # only in the delivered chat message; the outbox stores only its hash.
    confirmation_code = secrets.token_hex(3)
    claim_id = secrets.token_hex(16)
    claim = store.claim_request(
        task_id,
        version,
        claim_id=claim_id,
        confirm_code_hash=pc_store.code_hash(confirmation_code),
    )
    if not claim.get("acquired"):
        return _json(_public_result(claim, logical_task_id))
    content = _compose_confirmation_message(args, record, owner_name,
                                            confirmation_code)
    delivery = _deliver_to_current_chat(content, owner_staff_id)
    if "error" in delivery:
        if delivery.get("delivery_outcome") == "unknown":
            return _json(_public_result({
                **delivery,
                "ok": False,
                "reason": "REQUEST_OUTCOME_UNKNOWN",
                "request_state": pc_store.REQUEST_CLAIMED,
                "status": pc_store.PRODUCT_DRAFT,
                "retryable": False,
                "note": "delivery may have happened; the persistent claim is"
                        " retained for manual reconciliation, and another"
                        " request will not be sent automatically",
            }, logical_task_id))
        try:
            compensation = store.fail_request(
                task_id, version, claim_id, delivery["error"]
            )
        except Exception as exc:
            logger.exception("failed to persist product request compensation")
            return _json(_public_result({
                **delivery,
                "ok": False,
                "reason": "REQUEST_COMPENSATION_FAILED",
                "request_state": pc_store.REQUEST_CLAIMED,
                "status": pc_store.PRODUCT_DRAFT,
                "retryable": False,
                "note": "delivery was not accepted, but claim release could not"
                        f" be persisted ({type(exc).__name__}); no automatic retry",
            }, logical_task_id))
        return _json(_public_result({
            **delivery,
            "ok": False,
            "reason": "DELIVERY_FAILED",
            "request_state": compensation.get("request_state"),
            "status": pc_store.PRODUCT_DRAFT,
            "retryable": bool(compensation.get("retryable")),
            "note": "delivery was definitely rejected; state remains"
                    " PRODUCT_DRAFT and a later call may retry",
        }, logical_task_id))
    try:
        result = store.complete_request_delivery(
            task_id,
            version,
            claim_id=claim_id,
            delivery_ref=delivery.get("message_id", ""),
        )
    except Exception as exc:
        logger.exception("failed to finalize delivered product request")
        return _json(_public_result({
            "ok": False,
            "error": f"confirmation was delivered but finalization failed:"
                     f" {type(exc).__name__}",
            "reason": "REQUEST_FINALIZE_UNKNOWN",
            "request_state": pc_store.REQUEST_CLAIMED,
            "status": pc_store.PRODUCT_DRAFT,
            "retryable": False,
            "delivery": delivery,
            "note": "claim retained; reconcile before any retry to avoid a"
                    " duplicate confirmation message",
        }, logical_task_id))
    if not result.get("ok"):
        result.setdefault("delivery", delivery)
        result.setdefault("retryable", False)
        result.setdefault(
            "note",
            "message was delivered; claim retained and no automatic retry is"
            " allowed until the state is reconciled",
        )
        return _json(_public_result(result, logical_task_id))
    result.setdefault("delivery", delivery)
    result.setdefault(
        "note",
        "the owner's reply must quote the confirmation code from the chat"
        " message; pass it to product_confirm_decide as confirmation_code",
    )
    return _json(_public_result(result, logical_task_id))


# -- decide --------------------------------------------------------------------

PRODUCT_CONFIRM_DECIDE_SCHEMA = {
    "name": "product_confirm_decide",
    "description": (
        "Record the product owner's confirmation decision for a waiting task. "
        "Call this when the product owner replies to a confirmation request. "
        "The replier's identity is taken from the platform session (not from "
        "arguments) and must match the configured product owner; a non-owner "
        "reply, a stale proposal_version, a wrong/missing confirmation_code, "
        "a reply outside the task's origin conversation, or a conflicting "
        "repeat is rejected without changing state. confirmation_code is the "
        "one-time code the owner quotes from the confirmation message. "
        "decision: 'APPROVED' when the owner agrees to enter tech design, "
        "'NEEDS_REVISION' when they request changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "proposal_version": {"type": "string", "description": "The version the owner is confirming"},
            "decision": {"type": "string", "enum": ["APPROVED", "NEEDS_REVISION"]},
            "confirmation_code": {"type": "string", "description": "The one-time code quoted in the owner's reply"},
        },
        "required": ["task_id", "proposal_version", "decision",
                     "confirmation_code"],
    },
}


def _handle_decide(args: dict, **kw) -> str:
    logical_task_id = str(args.get("task_id") or "").strip()
    version = str(args.get("proposal_version") or "").strip()
    decision = str(args.get("decision") or "").strip()
    confirmation_code = str(args.get("confirmation_code") or "").strip()
    context = _dispatch_context.get()
    actor_id = context.actor_staff_id if context else ""
    if not actor_id:
        return _json({
            "ok": False, "applied": False, "reason": "NO_ACTOR_IDENTITY",
            "error": "the current session carries no platform staff id for the"
                     " replier; cannot verify the product owner — decision"
                     " rejected, state unchanged",
        })
    store = _get_store()
    task_id, record, resolution_error = _resolve_scoped_record(
        store, logical_task_id
    )
    if resolution_error:
        if resolution_error["reason"] == "WRONG_CONVERSATION":
            resolution_error["applied"] = False
        return _json(resolution_error)
    assert task_id is not None and record is not None
    owner_staff_id, _ = _resolve_owner()
    message_id = context.message_id if context else ""
    evidence = (
        f"dingtalk_message_id={message_id};"
        f"actor_sha={hashlib.sha256(actor_id.encode('utf-8')).hexdigest()[:12]}"
    )
    result = store.apply_decision(
        task_id, version, decision,
        actor_id=actor_id, owner_id=owner_staff_id, evidence=evidence,
        confirmation_code=confirmation_code,
    )
    if result.get("applied") and result.get("decision") == pc_store.DECISION_APPROVED:
        result["next"] = ("call product_confirm_advance to enter TECH_DESIGN;"
                          " coding is not allowed yet")
    if result.get("applied") and result.get("decision") == pc_store.DECISION_NEEDS_REVISION:
        result["next"] = ("revise the proposal and register it with"
                          " product_confirm_draft under a new proposal_version")
    return _json(_public_result(result, logical_task_id))


# -- advance -------------------------------------------------------------------

PRODUCT_CONFIRM_ADVANCE_SCHEMA = {
    "name": "product_confirm_advance",
    "description": (
        "Move an APPROVED task into TECH_DESIGN. This is the only path into "
        "tech design; it is rejected unless the task is PRODUCT_APPROVED."
    ),
    "parameters": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
}


def _handle_advance(args: dict, **kw) -> str:
    logical_task_id = str(args.get("task_id") or "").strip()
    store = _get_store()
    task_id, _, resolution_error = _resolve_scoped_record(
        store, logical_task_id
    )
    if resolution_error:
        return _json(resolution_error)
    assert task_id is not None
    return _json(_public_result(
        store.enter_tech_design(task_id), logical_task_id
    ))


# -- technical-design confirmation -------------------------------------------

_BUGFIX_ENVIRONMENTS = {"local", "dev", "uat", "production"}
_BUGFIX_LEVELS = {"B0", "B1", "B2", "B3"}
_CORE_MODULES = {
    "deep_search_or_search",
    "llm_or_prompt",
    "data_model_or_schema",
}


def _validate_tech_design_gate(
    args: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Build the durable non-coding gate snapshot or return a hard rejection."""
    work_kind = str(args.get("work_kind") or "").strip().lower()
    if work_kind not in {"feature", "bugfix"}:
        return None, {
            "ok": False,
            "reason": "INVALID_WORK_KIND",
            "error": "work_kind must be feature or bugfix",
        }
    base = {
        "work_kind": work_kind,
        "coding_allowed": False,
        "worker_allowed": False,
    }
    if work_kind == "feature":
        return base, None

    bugfix = args.get("bugfix_context")
    if not isinstance(bugfix, dict):
        return None, {
            "ok": False,
            "reason": "BUGFIX_GATE_INCOMPLETE",
            "error": "bugfix_context is required for a bugfix",
        }
    required = (
        "environment",
        "tenant_or_scope",
        "time_window",
        "reproduction_entry",
        "risk_level",
        "change_scope",
        "deliverable",
    )
    missing = [name for name in required if not str(bugfix.get(name) or "").strip()]
    if "core_modules" not in bugfix:
        missing.append("core_modules")
    if missing:
        return None, {
            "ok": False,
            "reason": "BUGFIX_GATE_INCOMPLETE",
            "missing": missing,
            "error": "bugfix environment gate is incomplete",
        }
    environment = str(bugfix["environment"]).strip().lower()
    risk_level = str(bugfix["risk_level"]).strip().upper()
    change_scope = str(bugfix["change_scope"]).strip().lower()
    deliverable = str(bugfix["deliverable"]).strip().upper()
    raw_core_modules = bugfix["core_modules"]
    if not isinstance(raw_core_modules, list):
        return None, {
            "ok": False,
            "reason": "INVALID_CORE_MODULES",
            "error": "core_modules must be an explicit array; use [] when none apply",
        }
    core_modules = sorted({
        str(item).strip().lower()
        for item in raw_core_modules
        if str(item).strip()
    })
    unknown_modules = sorted(set(core_modules) - _CORE_MODULES)
    if environment not in _BUGFIX_ENVIRONMENTS:
        return None, {
            "ok": False,
            "reason": "INVALID_BUGFIX_ENVIRONMENT",
            "error": "environment must be local, dev, uat or production",
        }
    if risk_level not in _BUGFIX_LEVELS:
        return None, {
            "ok": False,
            "reason": "INVALID_BUGFIX_LEVEL",
            "error": "risk_level must be B0, B1, B2 or B3",
        }
    expected_scope = {
        "B0": "non_behavioral",
        "B1": "single_surface",
        "B2": "cross_component",
    }.get(risk_level)
    if expected_scope and change_scope != expected_scope:
        return None, {
            "ok": False,
            "reason": "RISK_SCOPE_MISMATCH",
            "expected_change_scope": expected_scope,
            "error": f"{risk_level} requires change_scope={expected_scope}",
        }
    if risk_level == "B3" and change_scope not in {
        "core_module",
        "high_risk",
    }:
        return None, {
            "ok": False,
            "reason": "RISK_SCOPE_MISMATCH",
            "error": "B3 requires change_scope=core_module or high_risk",
        }
    if unknown_modules:
        return None, {
            "ok": False,
            "reason": "UNKNOWN_CORE_MODULE",
            "unknown_core_modules": unknown_modules,
        }
    if core_modules and risk_level != "B3":
        return None, {
            "ok": False,
            "reason": "CORE_MODULE_REQUIRES_B3",
            "error": "search, model/prompt, and data-model/schema changes are"
                     " structurally B3",
        }
    if risk_level == "B3" and deliverable != "RCA_PLAN_ONLY":
        return None, {
            "ok": False,
            "reason": "B3_PLAN_ONLY",
            "error": "B3 may only submit RCA_PLAN_ONLY for confirmation",
        }
    if risk_level != "B3" and deliverable not in {
        "TECHNICAL_DESIGN",
        "RCA_PLAN_ONLY",
    }:
        return None, {
            "ok": False,
            "reason": "INVALID_DELIVERABLE",
            "error": "deliverable must be TECHNICAL_DESIGN or RCA_PLAN_ONLY",
        }
    return {
        **base,
        "environment": environment,
        "tenant_or_scope": str(bugfix["tenant_or_scope"]).strip(),
        "time_window": str(bugfix["time_window"]).strip(),
        "reproduction_entry": str(bugfix["reproduction_entry"]).strip(),
        "risk_level": risk_level,
        "change_scope": change_scope,
        "core_modules": core_modules,
        "deliverable": deliverable,
    }, None


TECH_DESIGN_CONFIRM_DRAFT_SCHEMA = {
    "name": "tech_design_confirm_draft",
    "description": (
        "Register an immutable technical-design version after product approval. "
        "This is a non-coding gate: it never starts a coding worker. Bugfixes "
        "must explicitly provide environment, B0-B3 risk, change scope, an "
        "enumerated core_modules assessment (use [] only after checking), and "
        "deliverable structure; B3 and core-module changes are plan-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "tech_design_version": {"type": "string"},
            "tech_design_text": {"type": "string"},
            "tech_design_digest": {"type": "string"},
            "work_kind": {"type": "string", "enum": ["feature", "bugfix"]},
            "bugfix_context": {
                "type": "object",
                "properties": {
                    "environment": {
                        "type": "string",
                        "enum": ["local", "dev", "uat", "production"],
                    },
                    "tenant_or_scope": {"type": "string"},
                    "time_window": {"type": "string"},
                    "reproduction_entry": {"type": "string"},
                    "risk_level": {
                        "type": "string",
                        "enum": ["B0", "B1", "B2", "B3"],
                    },
                    "change_scope": {
                        "type": "string",
                        "enum": [
                            "non_behavioral",
                            "single_surface",
                            "cross_component",
                            "core_module",
                            "high_risk",
                        ],
                    },
                    "core_modules": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": sorted(_CORE_MODULES),
                        },
                    },
                    "deliverable": {
                        "type": "string",
                        "enum": ["TECHNICAL_DESIGN", "RCA_PLAN_ONLY"],
                    },
                },
                "required": [
                    "environment",
                    "tenant_or_scope",
                    "time_window",
                    "reproduction_entry",
                    "risk_level",
                    "change_scope",
                    "core_modules",
                    "deliverable",
                ],
            },
        },
        "required": ["task_id", "tech_design_version", "work_kind"],
    },
}


def _handle_tech_design_draft(args: dict, **kw) -> str:
    logical_task_id = str(args.get("task_id") or "").strip()
    version = str(args.get("tech_design_version") or "").strip()
    if not logical_task_id or not version:
        return _json({"ok": False,
                      "error": "task_id and tech_design_version are required"})
    text = str(args.get("tech_design_text") or "")
    digest = str(args.get("tech_design_digest") or "").strip() or (
        _digest(text) if text else ""
    )
    if not digest:
        return _json({"ok": False, "reason": "MISSING_DESIGN_DIGEST",
                      "error": "provide tech_design_text or tech_design_digest"})
    gate, gate_error = _validate_tech_design_gate(args)
    if gate_error:
        return _json(gate_error)
    store = _get_store()
    task_id, _, resolution_error = _resolve_scoped_record(
        store, logical_task_id
    )
    if resolution_error:
        return _json(resolution_error)
    assert task_id is not None and gate is not None
    result = store.create_tech_design_draft(
        task_id,
        version,
        digest,
        json.dumps(gate, ensure_ascii=False, sort_keys=True),
    )
    if result.get("ok"):
        result["gate"] = gate
    return _json(_public_result(result, logical_task_id))


TECH_DESIGN_CONFIRM_REQUEST_SCHEMA = {
    "name": "tech_design_confirm_request",
    "description": (
        "Send the current technical design to the configured owner for the "
        "second confirmation gate. Delivery is bound to the origin chat and "
        "uses the same durable one-time-code outbox as product confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "tech_design_version": {"type": "string"},
            "title": {"type": "string"},
            "design_summary": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "rollback_plan": {"type": "string"},
            "verification_plan": {"type": "string"},
        },
        "required": [
            "task_id",
            "tech_design_version",
            "title",
            "design_summary",
            "risks",
            "rollback_plan",
            "verification_plan",
        ],
    },
}


def _compose_tech_design_message(
    args: dict,
    record: Dict[str, Any],
    owner_name: str,
    confirmation_code: str,
) -> str:
    gate = json.loads(record["tech_design_gate_json"])
    risks = "\n".join(f"  - {item}" for item in (args.get("risks") or []))
    return (
        "### 技术设计确认请求\n"
        f"@{owner_name} 请确认以下技术设计。\n\n"
        f"- **需求**: {args['title']}\n"
        f"- **task_id**: `{record['logical_task_id']}`\n"
        f"- **技术设计版本**: `{record['tech_design_version']}`"
        f"（digest `{record['tech_design_digest']}`）\n"
        f"- **设计摘要**: {args['design_summary']}\n"
        f"- **结构化闸门**: `{json.dumps(gate, ensure_ascii=False, sort_keys=True)}`\n"
        f"- **风险**:\n{risks}\n"
        f"- **回滚方案**: {args['rollback_plan']}\n"
        f"- **验证方案**: {args['verification_plan']}\n\n"
        f"请 {owner_name} **在本群 @我 回复**其中之一，并带上技术设计确认码"
        f" `{confirmation_code}`：\n"
        f"1. **技术设计通过 {confirmation_code}**\n"
        f"2. **技术设计需修改 {confirmation_code}**（请说明修改点）\n\n"
        "此门只确认技术设计，不启动编码或 worker。"
    )


def _handle_tech_design_request(args: dict, **kw) -> str:
    logical_task_id = str(args.get("task_id") or "").strip()
    version = str(args.get("tech_design_version") or "").strip()
    store = _get_store()
    task_id, record, resolution_error = _resolve_scoped_record(
        store, logical_task_id
    )
    if resolution_error:
        return _json(resolution_error)
    assert task_id is not None and record is not None
    if (
        record["status"] != pc_store.TECH_DESIGN
        or record["tech_design_version"] != version
    ):
        return _json({
            "ok": False,
            "reason": "NOT_CURRENT_TECH_DESIGN",
            "status": record["status"],
            "current_version": record["tech_design_version"],
        })
    owner_staff_id, owner_name = _resolve_owner()
    if not owner_staff_id:
        return _json({"ok": False, "reason": "OWNER_NOT_CONFIGURED",
                      "error": "product owner staff id is not configured"})
    confirmation_code = secrets.token_hex(3)
    claim_id = secrets.token_hex(16)
    claim = store.claim_tech_design_request(
        task_id,
        version,
        claim_id,
        pc_store.code_hash(confirmation_code),
    )
    if not claim.get("acquired"):
        return _json(_public_result(claim, logical_task_id))
    content = _compose_tech_design_message(
        args, record, owner_name, confirmation_code
    )
    delivery = _deliver_to_current_chat(content, owner_staff_id)
    if "error" in delivery:
        if delivery.get("delivery_outcome") == "unknown":
            return _json({
                **delivery,
                "ok": False,
                "reason": "REQUEST_OUTCOME_UNKNOWN",
                "request_state": pc_store.REQUEST_CLAIMED,
                "status": pc_store.TECH_DESIGN,
                "retryable": False,
                "coding_allowed": False,
                "worker_allowed": False,
            })
        try:
            compensation = store.fail_tech_design_request(
                task_id, version, claim_id, delivery["error"]
            )
        except Exception as exc:
            logger.exception(
                "failed to persist technical-design request compensation"
            )
            return _json({
                **delivery,
                "ok": False,
                "reason": "REQUEST_COMPENSATION_FAILED",
                "request_state": pc_store.REQUEST_CLAIMED,
                "status": pc_store.TECH_DESIGN,
                "retryable": False,
                "coding_allowed": False,
                "worker_allowed": False,
                "note": f"claim retained after {type(exc).__name__}",
            })
        return _json({
            **delivery,
            "ok": False,
            "reason": "DELIVERY_FAILED",
            "request_state": compensation.get("request_state"),
            "status": pc_store.TECH_DESIGN,
            "retryable": bool(compensation.get("retryable")),
            "coding_allowed": False,
            "worker_allowed": False,
        })
    try:
        result = store.complete_tech_design_request_delivery(
            task_id,
            version,
            claim_id,
            delivery.get("message_id", ""),
        )
    except Exception as exc:
        logger.exception("failed to finalize technical-design request")
        return _json({
            "ok": False,
            "reason": "REQUEST_FINALIZE_UNKNOWN",
            "request_state": pc_store.REQUEST_CLAIMED,
            "status": pc_store.TECH_DESIGN,
            "retryable": False,
            "delivery": delivery,
            "coding_allowed": False,
            "worker_allowed": False,
            "note": f"claim retained after {type(exc).__name__}",
        })
    result["delivery"] = delivery
    result["coding_allowed"] = False
    result["worker_allowed"] = False
    return _json(_public_result(result, logical_task_id))


TECH_DESIGN_CONFIRM_DECIDE_SCHEMA = {
    "name": "tech_design_confirm_decide",
    "description": (
        "Record the configured owner's decision at the second, technical-design "
        "gate. Identity comes only from the live platform event; wrong session, "
        "missing identity, stale version, wrong code and conflicting repeats "
        "fail closed. Approval remains a non-coding terminal state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "tech_design_version": {"type": "string"},
            "decision": {
                "type": "string",
                "enum": ["APPROVED", "NEEDS_REVISION"],
            },
            "confirmation_code": {"type": "string"},
        },
        "required": [
            "task_id",
            "tech_design_version",
            "decision",
            "confirmation_code",
        ],
    },
}


def _handle_tech_design_decide(args: dict, **kw) -> str:
    logical_task_id = str(args.get("task_id") or "").strip()
    version = str(args.get("tech_design_version") or "").strip()
    decision = str(args.get("decision") or "").strip()
    confirmation_code = str(args.get("confirmation_code") or "").strip()
    context = _dispatch_context.get()
    actor_id = context.actor_staff_id if context else ""
    if not actor_id:
        return _json({"ok": False, "applied": False,
                      "reason": "NO_ACTOR_IDENTITY",
                      "error": "cannot verify the configured owner"})
    store = _get_store()
    task_id, _, resolution_error = _resolve_scoped_record(
        store, logical_task_id
    )
    if resolution_error:
        resolution_error["applied"] = False
        return _json(resolution_error)
    assert task_id is not None
    owner_staff_id, _ = _resolve_owner()
    message_id = context.message_id if context else ""
    evidence = (
        f"dingtalk_message_id={message_id};"
        f"actor_sha={hashlib.sha256(actor_id.encode('utf-8')).hexdigest()[:12]}"
    )
    result = store.apply_tech_design_decision(
        task_id,
        version,
        decision,
        actor_id,
        owner_staff_id,
        evidence,
        confirmation_code,
    )
    if result.get("applied"):
        result["next"] = (
            "technical design is confirmed; this plugin has no coding or"
            " worker transition"
            if result.get("decision") == pc_store.DECISION_APPROVED
            else "revise and register a new technical-design version"
        )
    return _json(_public_result(result, logical_task_id))


# -- status --------------------------------------------------------------------

PRODUCT_CONFIRM_STATUS_SCHEMA = {
    "name": "product_confirm_status",
    "description": (
        "Inspect product-confirmation state. With task_id: full record, "
        "version history and decision log (low-sensitivity hashes only). "
        "Without task_id: all tasks still WAITING_PRODUCT_CONFIRMATION — call "
        "this after a restart to recover pending confirmations."
    ),
    "parameters": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": [],
    },
}


def _redact(record: Dict[str, Any]) -> Dict[str, Any]:
    # The code hash must never reach the model: revealing it would let a
    # motivated caller offline-brute-force the short one-time code.
    return {
        k: v for k, v in _public_record(record).items()
        if k not in {"confirm_code_hash", "tech_confirm_code_hash"}
    }


def _handle_status(args: dict, **kw) -> str:
    store = _get_store()
    logical_task_id = str(args.get("task_id") or "").strip()
    if logical_task_id:
        task_id, record, resolution_error = _resolve_scoped_record(
            store, logical_task_id
        )
        if resolution_error:
            return _json(resolution_error)
        assert task_id is not None and record is not None
        versions = store.list_versions(task_id)
        tech_versions = store.list_tech_design_versions(task_id)
        for item in versions + tech_versions:
            item["task_id"] = logical_task_id
        product_request = store.get_request_status(
            task_id, record["proposal_version"]
        )
        if product_request:
            product_request = _public_result(
                product_request, logical_task_id
            )
        tech_request = (
            store.get_tech_design_request_status(
                task_id, record["tech_design_version"]
            )
            if record.get("tech_design_version")
            else None
        )
        if tech_request:
            tech_request = _public_result(tech_request, logical_task_id)
        decision_log = store.decision_history(task_id)
        for item in decision_log:
            item["task_id"] = logical_task_id
        return _json({
            "record": _redact(record),
            "request": product_request,
            "tech_design_request": tech_request,
            "versions": versions,
            "tech_design_versions": tech_versions,
            "decision_log": decision_log,
        })
    current_conversation = _current_conversation()
    pending = [
        _redact(r)
        for r in store.list_pending(current_conversation)
    ]
    pending_tech = [
        _redact(r)
        for r in store.list_pending_tech_design(current_conversation)
    ]
    return _json({"waiting_product_confirmation": pending,
                  "waiting_tech_design_confirmation": pending_tech,
                  "count": len(pending) + len(pending_tech),
                  "unsettled_request_count":
                      len(store.list_unsettled_requests(
                          current_conversation
                      ))})
