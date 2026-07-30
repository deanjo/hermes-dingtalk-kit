"""DingTalk task binding V2: explicit ``#任务`` prefix parsing, the read-only
binding-materialize bridge, and the H1 thin-gate fact store.

V2 (H1_V2_BARE_AGENT_DESIGN): the intent-classifier bridge, quote registry,
pending withdrawal chain, refusal receipts, and the V1 C1 proposal slot /
delivery-callback machinery were all removed — the main agent is the only
brain.  What remains:

* ``#任务 board/task message`` structural parsing (unchanged),
* ``materialize_natural_binding`` — restore the durable current binding
  onto the source (read-only; the thin gate degrades to unbound delivery
  on failure, M3),
* the H1 fact store — bounded in-memory records for declared proposals
  (``h1_declare_proposal``), their delivery flags, and last-delivered-reply
  receipts, plus the per-turn dispatch scope used to correlate them.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_PREFIX_RE = re.compile(r"^\s*#任务\s+(\S+)\s+(.+?)\s*$", re.DOTALL)
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_RE = re.compile(r"^t_[0-9a-f]{4,64}$")


class TaskBindingError(ValueError):
    """A message declared a task binding but it was malformed."""


@dataclass(frozen=True)
class TaskBinding:
    board_slug: str
    task_id: str


@dataclass(frozen=True)
class ParsedTaskMessage:
    binding: TaskBinding | None
    message_text: str


def parse_task_binding(text: str) -> ParsedTaskMessage:
    """Parse ``#任务 board/task message`` only at the message start."""
    value = text or ""
    if not value.lstrip().startswith("#任务"):
        return ParsedTaskMessage(binding=None, message_text=value)
    match = _PREFIX_RE.fullmatch(value)
    if match is None:
        raise TaskBindingError("expected '#任务 board/task message'")
    reference, message_text = match.groups()
    if reference.count("/") != 1:
        raise TaskBindingError("task reference must be board/task")
    board_slug, task_id = reference.split("/", 1)
    if not _BOARD_RE.fullmatch(board_slug):
        raise TaskBindingError("invalid board slug")
    if not _TASK_RE.fullmatch(task_id):
        raise TaskBindingError("invalid task id")
    if not message_text.strip():
        raise TaskBindingError("message text is required")
    return ParsedTaskMessage(
        binding=TaskBinding(board_slug=board_slug, task_id=task_id),
        message_text=message_text.strip(),
    )


def task_binding_exists(board_slug: str, task_id: str) -> bool:
    """Read the selected board without creating or migrating a database."""
    try:
        from hermes_cli import kanban_db as kb

        normalized = kb._normalize_board_slug(board_slug)
        if normalized != board_slug:
            return False
        db_path = kb.kanban_db_path(board=normalized)
        if not db_path.is_file():
            return False
        uri = f"file:{db_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? LIMIT 1", (task_id,)
            ).fetchone()
        return row is not None
    except (ImportError, OSError, sqlite3.Error, ValueError):
        return False


async def resolve_task_binding(
    adapter: object,
    text: str,
    *,
    chat_id: str,
    message_id: str,
    clarification: str,
) -> ParsedTaskMessage | None:
    """Resolve a prefix or send exactly one clarification and stop."""
    try:
        parsed = parse_task_binding(text)
    except TaskBindingError:
        parsed = None
    if parsed is not None and (
        parsed.binding is None
        # Synchronous SQLite probe (default 5s lock timeout) — keep it off
        # the event loop, same as the materialize bridge below.
        or await asyncio.to_thread(
            task_binding_exists, parsed.binding.board_slug, parsed.binding.task_id
        )
    ):
        return parsed
    await adapter.send(
        chat_id,
        clarification,
        reply_to=message_id,
        metadata={"delivery_class": "business_error"},
    )
    return None


def resolve_gateway_profile() -> str | None:
    """Profile whose runtime scope constructed this adapter (multiplex).

    A multiplex gateway constructs each secondary profile's adapters under
    that profile's HERMES_HOME scope and the primary adapter under the
    process HERMES_HOME, so the active profile name resolved at construction
    time is exactly the profile the gateway stamps on ``source.profile`` /
    falls back to for session and task-state keys. None => unknown. With
    multiplexing off, the resolved value (usually ``"default"``) is inert
    for session/task-state keys — they stay byte-identical — though a
    serialized source may then carry ``"profile": "default"``.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or None
    except Exception:  # noqa: BLE001 - profile discovery must never break startup
        return None


async def materialize_natural_binding(adapter: object, source: object):
    """Restore the current task binding onto ``source`` (read-only, D3).

    The thin gate runs this for every intake-eligible message so a bound
    user's message always carries its task context.  Core's
    ``materialize_current_task_source`` never creates or consumes anything
    and returns the source unchanged when there is no current binding.
    Same profile stamping + ``asyncio.to_thread`` offload as before.
    """
    from gateway.task_intake import materialize_current_task_source

    if not getattr(source, "profile", None):
        source.profile = adapter._gateway_profile
    return await asyncio.to_thread(
        materialize_current_task_source,
        adapter._session_store,
        source,
    )


# ---------------------------------------------------------------------------
# H1 V2 fact store (§3.1/§3.2): declared proposals, delivery flags, reply
# receipts, and the per-turn dispatch scope.  In-memory only — a restart
# invalidates undeclared proposals (safe direction, R C).  Bounded dicts +
# TTL 900s + threading.Lock; no schema changes, no state.db writes.
# ---------------------------------------------------------------------------

_H1_FACT_TTL_SECONDS = 900.0
_H1_FACT_MAX_KEYS = 256
_H1_WRITE_MAX_RETRIES = 3

_h1_fact_lock = threading.Lock()

# Per-turn dispatch scope, published by the thin gate for every dispatched
# message (ContextVar: the background processing task inherits a copy at
# creation; delivery points and tool workers see exactly this turn).
_h1_dispatch_scope: contextvars.ContextVar = contextvars.ContextVar(
    "h1_v2_dispatch_scope", default=None
)


def set_h1_dispatch_scope(*, source, text, message_id):
    _h1_dispatch_scope.set(
        {"source": source, "text": text, "message_id": message_id}
    )


def h1_dispatch_scope():
    return _h1_dispatch_scope.get()


def is_h1_failure_receipt() -> bool:
    """True when the turn's outgoing reply was stamped as a failure receipt
    (Core ``gateway.honest_failure``, R3).  An old Core without the helper
    fails closed (treated as stamped)."""
    try:
        from gateway.honest_failure import is_failure_receipt

        return bool(is_failure_receipt())
    except Exception:  # noqa: BLE001 - no mark readable => fail closed
        return True


async def restore_h1_binding(adapter: object, source: object):
    """Thin-gate binding restore (D3/M3): materialize the durable binding,
    degrade to UNBOUND delivery on any failure (never drop the message)."""
    try:
        materialized = await materialize_natural_binding(adapter, source)
    except Exception:
        logger.warning(
            "[%s] Failed to restore current task binding; delivering unbound (M3)",
            getattr(adapter, "name", "dingtalk"),
            exc_info=True,
        )
        return source
    return materialized if materialized is not None else source


def h1_turn_meta_lines(adapter: object, *, chat_id, sender_user, msg_id) -> list:
    """§3.1 structural meta for this inbound turn (code-assembled facts,
    zero semantics): the inbound message number, plus the previous-turn
    delivery receipt when the inbound sender IS its triggering user (M5)."""
    lines = []
    if msg_id:
        lines.append(f"[消息编号: {msg_id}]")
    receipt = last_delivered_reply(adapter, chat_id=chat_id, user=sender_user)
    if receipt and receipt.get("msgId"):
        lines.append(f"[你上一条回复已送达，编号: {receipt['msgId']}]")
    return lines


def _scope_identity(source) -> str:
    """Stable display key for a user identity (user_id preferred)."""
    user_id = str(getattr(source, "user_id", None) or "").strip()
    user_id_alt = str(getattr(source, "user_id_alt", None) or "").strip()
    return user_id or user_id_alt


def _scope_chat_id(source) -> str:
    return str(getattr(source, "chat_id", None) or "")


def _facts(adapter) -> dict:
    facts = getattr(adapter, "_h1_facts", None)
    if facts is None:
        facts = adapter._h1_facts = {"proposals": {}, "pending_delivery": {}, "replies": {}}
    return facts


def _sweep(facts: dict, now: float) -> None:
    for key in [k for k, r in facts["proposals"].items() if r["ts"] + _H1_FACT_TTL_SECONDS <= now]:
        facts["proposals"].pop(key, None)
    for key in [k for k, r in facts["pending_delivery"].items() if r["ts"] + _H1_FACT_TTL_SECONDS <= now]:
        facts["pending_delivery"].pop(key, None)
    for key in [k for k, r in facts["replies"].items() if r["ts"] + _H1_FACT_TTL_SECONDS <= now]:
        facts["replies"].pop(key, None)


def declare_h1_proposal(adapter, record: dict) -> str | None:
    """Store one declared proposal and mark it awaiting delivery.

    Returns None on success, or ``"proposal_in_flight"`` when the same
    (chat, user) already has an undeclared-yet-undelivered proposal (one
    proposal per turn — the model reuses the outstanding one).
    """
    now = time.time()
    with _h1_fact_lock:
        facts = _facts(adapter)
        _sweep(facts, now)
        slot_key = (record["chat_id"], record["triggering_user"])
        if slot_key in facts["pending_delivery"]:
            return "proposal_in_flight"
        if len(facts["proposals"]) >= _H1_FACT_MAX_KEYS:
            facts["proposals"].pop(next(iter(facts["proposals"])), None)
        facts["proposals"][record["proposal_id"]] = record
        facts["pending_delivery"][slot_key] = {
            "proposal_id": record["proposal_id"],
            "ts": record["ts"],
        }
        return None


def get_h1_proposal(adapter, proposal_id: str) -> dict | None:
    # Readers never purge: an EXPIRED record must stay distinguishable
    # (proposal_expired) from an unknown one — sweeping happens on writes
    # (declare/mark/consume-flip) to keep the maps bounded.
    with _h1_fact_lock:
        return _facts(adapter)["proposals"].get(proposal_id)


def mark_h1_turn_delivered(adapter, *, chat_id, message_id) -> None:
    """Delivery record point (§3.1-2/§3.2): on a provably successful final
    reply (card finalize/send OR webhook fallback — I1: the kit knows the
    outcome; the synthetic webhook id is fine because V2 does no quote
    authentication), flag this turn's declared proposal ``delivered`` and
    record the reply receipt keyed by (chat_id, triggering_user).
    A stamped failure receipt (R3) is never recorded — but it still releases
    this turn's ``pending_delivery`` slot: "the proposal is no longer in
    flight" is a concurrency fact, separate from "this reply counts as the
    agent's final answer".
    """
    try:
        from gateway.honest_failure import is_failure_receipt
    except Exception:  # noqa: BLE001 - old Core without the helper: fail closed
        is_failure_receipt = lambda: True
    failed = is_failure_receipt()
    if not failed and not message_id:
        return
    scope = _h1_dispatch_scope.get()
    if not scope or scope.get("source") is None:
        return
    triggering_user = _scope_identity(scope["source"])
    if not triggering_user:
        return
    now = time.time()
    with _h1_fact_lock:
        facts = _facts(adapter)
        # Sweep only the delivery/receipt maps here: an expired PROPOSAL
        # must survive to the write gate so it reports proposal_expired
        # rather than proposal_unknown (declare-time sweeps keep the map
        # bounded).
        for key in [k for k, r in facts["pending_delivery"].items() if r["ts"] + _H1_FACT_TTL_SECONDS <= now]:
            facts["pending_delivery"].pop(key, None)
        for key in [k for k, r in facts["replies"].items() if r["ts"] + _H1_FACT_TTL_SECONDS <= now]:
            facts["replies"].pop(key, None)
        slot_key = (str(chat_id or _scope_chat_id(scope["source"])), triggering_user)
        # Release unconditionally — the slot only says "this turn's proposal
        # is still in flight".  Holding it through a failure receipt leaked it
        # until the TTL: the user's 确认 kept failing (not_delivered) and every
        # later declare was refused (proposal_in_flight), so the model would
        # announce a pending proposal the user had never been shown.
        slot = facts["pending_delivery"].pop(slot_key, None)
        if failed:
            return  # R3: neither delivered=True nor a reply receipt
        if slot is not None:
            record = facts["proposals"].get(slot["proposal_id"])
            if record is not None:
                record["delivered"] = True
        if len(facts["replies"]) >= _H1_FACT_MAX_KEYS:
            facts["replies"].pop(next(iter(facts["replies"])), None)
        facts["replies"][slot_key] = {"msgId": message_id, "ts": now}


def last_delivered_reply(adapter, *, chat_id, user) -> dict | None:
    """The most recent delivered-reply receipt for (chat_id, user) — the
    ``[你上一条回复已送达，编号: …]`` injection reads it (M5: injected only
    when the inbound sender IS the triggering user)."""
    with _h1_fact_lock:
        facts = _facts(adapter)
        _sweep(facts, time.time())
        return facts["replies"].get((str(chat_id or ""), str(user or "")))


def try_consume_h1_proposal(adapter, proposal_id: str) -> str | None:
    """I2 三态消费（先消费后写）：declared→consumed under the lock.

    Returns None when the flip succeeded; otherwise the rejection reason:
    ``proposal_unknown`` / ``not_delivered`` / ``proposal_expired`` /
    ``proposal_already_consumed`` / ``write_retry_exhausted``.  The lock
    only guards the flag flip (milliseconds) — the business write happens
    outside it, so the lock never spans I/O.  Expiry is checked on the
    record itself (never swept away first) so it reports distinctly.
    """
    now = time.time()
    with _h1_fact_lock:
        facts = _facts(adapter)
        record = facts["proposals"].get(proposal_id)
        if record is None:
            return "proposal_unknown"
        if not record.get("delivered"):
            return "not_delivered"
        if record["ts"] + _H1_FACT_TTL_SECONDS <= now:
            return "proposal_expired"
        if record.get("state") == "consumed":
            return "proposal_already_consumed"
        if int(record.get("retries") or 0) >= _H1_WRITE_MAX_RETRIES:
            return "write_retry_exhausted"
        record["state"] = "consumed"
        record["retries"] = int(record.get("retries") or 0) + 1
        return None


def rollback_h1_consumption(adapter, proposal_id: str) -> None:
    """outcome_unknown rollback (I2): allow the same proposal_id to re-enter
    — the idempotency key deduplicates an already-committed write."""
    with _h1_fact_lock:
        facts = _facts(adapter)
        record = facts["proposals"].get(proposal_id)
        if record is not None and record.get("state") == "consumed":
            record["state"] = "declared"
