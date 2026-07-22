"""DingTalk task binding: explicit ``#任务`` prefix parsing plus the natural
task intake seam (quote-aware confirmation R9 #3 hardened by R2 C1/C2 —
prompts registered and matched as full ``(operation_id, phase,
target_digest)`` triples with explicit quote authentication, media binding
restore R9 #6, sender/inbound-msgId fail-closed R9 #9 + R2)."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
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
        # the event loop, same as the natural resolver below.
        or await asyncio.to_thread(
            task_binding_exists, parsed.binding.board_slug, parsed.binding.task_id
        )
    ):
        return parsed
    await adapter.send(chat_id, clarification, reply_to=message_id)
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


async def resolve_natural_intake(
    adapter: object,
    source: object,
    text: str,
    message_id: str,
    *,
    quote: dict | None = None,
):
    """Resolve one natural message via Core task intake, off the event loop.

    The Core resolver keys task state on ``source.profile``, but the gateway
    stamps the profile only after the adapter hands over the event — so the
    adapter's owning profile is stamped here first, and two multiplex
    profiles in the same chat never share task state. With multiplexing off
    the stamp leaves every session/task-state key byte-identical (only a
    serialized source may gain ``"profile": "default"``). The resolver
    itself is synchronous (Kanban scan + SQLite lock waits), so it is
    offloaded with ``asyncio.to_thread``; the return value and exception
    propagation match a direct synchronous call.

    ``quote`` is the R9 #3 quote-awareness payload (D6), hardened in R2:
    ``None`` for an ordinary message, otherwise ``{"replied_message_id":
    str | None, "authenticated": bool, "matched": {"operation_id", "phase",
    "target_digest"} | None, "quoted_text": str | None}`` — which bot prompt
    (if any) the user replied to and whether that prompt is a registered,
    unexpired confirmation for this chat. Core decides whether a control
    intent may consume the pending; the Kit never interprets it.
    """
    from gateway.task_intake import resolve_natural_task_intake

    if not getattr(source, "profile", None):
        source.profile = adapter._gateway_profile
    return await asyncio.to_thread(
        resolve_natural_task_intake,
        adapter._session_store,
        source,
        text,
        message_id,
        enabled=True,
        quote=quote,
    )


async def materialize_natural_binding(adapter: object, source: object):
    """Restore the current task binding onto ``source`` for a media message.

    Media bypasses the candidate/confirmation intake (a clarification branch
    would drop the attachment), but a bound user's media must still carry its
    task context to the gateway (R9 #6, D7). Core's
    ``materialize_current_task_source`` is read-only — it never creates or
    consumes a pending — and returns the source unchanged when there is no
    current binding. Same profile stamping + ``asyncio.to_thread`` offload as
    ``resolve_natural_intake``.
    """
    from gateway.task_intake import materialize_current_task_source

    if not getattr(source, "profile", None):
        source.profile = adapter._gateway_profile
    return await asyncio.to_thread(
        materialize_current_task_source,
        adapter._session_store,
        source,
    )


# R9 #3 (D6) quote-aware confirmation, hardened by R2 C1/C2: registry of
# outbound confirmation prompts, chat_id -> {message_id: (operation_id,
# phase, target_digest, expires_at)} — the full pending triple, so a quote
# only authenticates the exact phase it was prompted for. The TTL must
# outlive the Core pending it points at (AWAITING_TTL_SECONDS=900) so a quote
# arriving near pending expiry still matches and lets Core decide; the
# per-chat FIFO cap keeps a busy group from growing the map without bound.
# In-memory only (accepted residual R5): a restart degrades a quoted confirm
# to the T1 reply clarification — the safe direction, never a wrong consume.
_INTAKE_PROMPT_REGISTRY_TTL_SECONDS = 1200.0
_INTAKE_PROMPT_REGISTRY_MAX_PER_CHAT = 8


def register_intake_prompt(
    registry, chat_id, message_id, operation_id, phase, target_digest, *, now=None
):
    """Record that outbound ``message_id`` carried the confirmation prompt for
    the ``(operation_id, phase, target_digest)`` triple (expiry in monotonic
    seconds)."""
    if not chat_id or not message_id or not operation_id:
        return
    now = time.monotonic() if now is None else now
    _purge_expired_intake_prompts(registry, chat_id, now)
    entries = registry.setdefault(chat_id, {})
    while len(entries) >= _INTAKE_PROMPT_REGISTRY_MAX_PER_CHAT:
        evicted_id = next(iter(entries))
        entries.pop(evicted_id, None)
        logger.warning(
            "Intake prompt registry at capacity (%d) for chat; evicted oldest entry has_message_id=%s",
            _INTAKE_PROMPT_REGISTRY_MAX_PER_CHAT,
            bool(evicted_id),
        )
    entries[message_id] = (
        operation_id,
        phase,
        target_digest,
        now + _INTAKE_PROMPT_REGISTRY_TTL_SECONDS,
    )


def _purge_expired_intake_prompts(registry, chat_id, now):
    """Drop expired entries for ``chat_id`` and remove the chat key once its
    map is empty, so long-lived processes don't accumulate dead chats."""
    entries = registry.get(chat_id)
    if not entries:
        registry.pop(chat_id, None)
        return
    for stale_id in [key for key, entry in entries.items() if entry[-1] <= now]:
        entries.pop(stale_id, None)
    if not entries:
        registry.pop(chat_id, None)


def match_intake_prompt(registry, chat_id, message_id, *, now=None):
    """Return the ``{"operation_id", "phase", "target_digest"}`` triple whose
    confirmation prompt was ``message_id``, or None when the quoted message
    was never registered (or has expired)."""
    if not chat_id or not message_id:
        return None
    now = time.monotonic() if now is None else now
    entries = registry.get(chat_id) or {}
    hit = entries.get(message_id)
    if hit is None:
        return None
    operation_id, phase, target_digest, expires_at = hit
    if expires_at <= now:
        _purge_expired_intake_prompts(registry, chat_id, now)
        return None
    return {
        "operation_id": operation_id,
        "phase": phase,
        "target_digest": target_digest,
    }


def register_delivered_prompt(registry, chat_id, intake, send_result, *, now=None):
    """Register a confirmation prompt after it was actually delivered (D6).

    Only a ``reply_without_agent`` carrying a complete prompt triple
    (``prompt_operation_id`` + ``prompt_phase`` + ``prompt_target_digest``)
    whose send succeeded with a real outbound message id is registered;
    anything else (errors, legacy results, failed sends, incomplete triples)
    is a no-op — a quote of that prompt then simply stays unauthenticated.
    """
    if getattr(intake, "action", None) != "reply_without_agent":
        return
    operation_id = getattr(intake, "prompt_operation_id", None)
    phase = getattr(intake, "prompt_phase", None)
    target_digest = getattr(intake, "prompt_target_digest", None)
    message_id = getattr(send_result, "message_id", None)
    if (
        not operation_id
        or not phase
        or not target_digest
        or not getattr(send_result, "success", False)
        or not message_id
    ):
        return
    register_intake_prompt(
        registry, chat_id, message_id, operation_id, phase, target_digest, now=now
    )


def build_intake_quote(registry, chat_id, message):
    """Assemble the quote payload for Core, or None for a non-quote message.

    Every quote-reply yields a payload with an explicit authentication
    verdict (R2 C1/C2):

    * ``authenticated=True`` and ``matched`` = the registered
      ``{"operation_id", "phase", "target_digest"}`` triple — only when the
      quoted message is a registered, unexpired confirmation prompt of this
      chat.
    * ``authenticated=False`` and ``matched=None`` for everything else:
      unknown/expired/another chat's message, a ``repliedMsg`` without
      ``msgId``, or a webhook-delivered prompt (its ``SendResult.message_id``
      is a locally synthesized uuid DingTalk never echoes back, so it can
      never match). ``quoted_text`` still rides along for Core to display —
      it never authenticates anything.
    """
    try:
        from .reply_context import _extract_replied_text_original, _get_text_extensions
    except ImportError:  # standalone (non-package) module load
        from reply_context import _extract_replied_text_original, _get_text_extensions  # type: ignore

    replied = (_get_text_extensions(message) or {}).get("repliedMsg")
    if not isinstance(replied, dict) or not replied:
        return None
    replied_message_id = str(replied.get("msgId") or replied.get("msgid") or "").strip()
    matched = None
    if replied_message_id:
        matched = match_intake_prompt(registry, chat_id, replied_message_id)
    return {
        "replied_message_id": replied_message_id or None,
        "authenticated": matched is not None,
        "matched": matched,
        "quoted_text": _extract_replied_text_original(replied) or None,
    }


@dataclass(frozen=True)
class NaturalIntakeGateResult:
    """Outcome of the natural-intake gate for one inbound message."""

    handled: bool  # True => the adapter must stop (a reply was sent / error)
    source: object = None
    text: str = ""
    control_consumed: bool = False


def has_stable_message_id(message: object) -> bool:
    """True when the raw inbound message carries a platform-stable msgId.

    ``_on_message`` synthesizes a random UUID for dedup when the platform
    delivers none; that UUID must never double as the natural-intake request
    number, so the gate reads the id off the raw message instead of trusting
    the (possibly synthesized) ``message_id`` it is handed.
    """
    return bool(str(getattr(message, "message_id", None) or "").strip())


async def run_natural_intake_gate(
    adapter: object,
    source: object,
    text: str,
    message_id: str,
    chat_id: str,
    message: object,
    *,
    media_urls: list,
    has_stable_sender: bool,
    task_binding: object,
) -> NaturalIntakeGateResult:
    """Natural task intake gate for one inbound DingTalk message.

    Structural short-circuits (feature flag, slash commands, ``#任务`` Raw
    binding) stay here with the R9/R2 fixes so ``_on_message`` keeps a single
    call site:

    * R9 #6 (D7): media never enters candidate/confirmation intake, but a
      bound user's media keeps its task context via Core's read-only
      ``materialize_current_task_source``; a restore failure logs a warning
      and the media is delivered unbound — never dropped.
    * R9 #9 (D10): without a stable sender id the gate fails closed — no
      pending is created or consumed; the message still reaches the agent.
    * R2: without a platform-stable inbound message id the gate also fails
      closed — a random UUID must never serve as the intake request number,
      so the resolver is skipped entirely (no pending created or consumed)
      and the message flows on normally.
    * R9 #3 (D6) + R2 C1/C2: a quote-reply is resolved through
      ``build_intake_quote``; Core decides whether a control intent may
      consume that operation. A delivered confirmation prompt is registered
      by its outbound message id together with the full prompt triple
      (``register_delivered_prompt``) so a later quote can only authenticate
      the exact phase it replied to.

    ``handled=True`` means a reply was already sent (or the resolver failed
    after the user was told) and the adapter must return. Otherwise the
    (possibly re-bound) source, the text to dispatch, and whether a control
    message was consumed are handed back — a consumed control makes the
    adapter drop the reply context so the T1 clarification stays silent.
    """
    if (adapter.config.extra or {}).get("natural_task_intake") is not True:
        return NaturalIntakeGateResult(handled=False, source=source, text=text)
    if task_binding is not None:
        return NaturalIntakeGateResult(handled=False, source=source, text=text)
    if media_urls:
        # v1: any media bypasses task intake — a clarification branch would
        # drop the attachment. Slash commands stay fully structural.
        if has_stable_sender and not (text or "").lstrip().startswith("/"):
            try:
                materialized = await materialize_natural_binding(adapter, source)
            except Exception:
                logger.warning(
                    "[%s] Failed to restore current task binding for media message; delivering unbound",
                    getattr(adapter, "name", "dingtalk"),
                    exc_info=True,
                )
            else:
                if materialized is not None:
                    source = materialized
        return NaturalIntakeGateResult(handled=False, source=source, text=text)
    if not (text or "").strip() or (text or "").lstrip().startswith("/"):
        return NaturalIntakeGateResult(handled=False, source=source, text=text)
    if not has_stable_sender:
        # R9 #9 (D10): fail closed — no pending for an unidentifiable sender;
        # the message still reaches the agent main loop.
        logger.warning(
            "[%s] Natural task intake skipped: no stable sender identity",
            getattr(adapter, "name", "dingtalk"),
        )
        return NaturalIntakeGateResult(handled=False, source=source, text=text)
    if not has_stable_message_id(message):
        # R2: fail closed — without a platform-stable inbound msgId the intake
        # request number would have to be the random UUID ``_on_message``
        # synthesizes for dedup, so no pending may be created or consumed;
        # the message still reaches the agent main loop.
        logger.warning(
            "[%s] Natural task intake skipped: no stable inbound message id",
            getattr(adapter, "name", "dingtalk"),
        )
        return NaturalIntakeGateResult(handled=False, source=source, text=text)
    quote = build_intake_quote(adapter._intake_prompt_msgs, chat_id, message)
    try:
        intake = await resolve_natural_intake(adapter, source, text or "", message_id, quote=quote)
        if intake.action not in {"pass_through", "reply_without_agent", "bound_source", "error_without_agent"}:
            raise ValueError(f"Unknown natural task intake action: {intake.action!r}")
    except Exception:
        logger.exception("[%s] Natural task intake failed", getattr(adapter, "name", "dingtalk"))
        await adapter.send(chat_id, "任务接入暂时不可用，请稍后重试。", reply_to=message_id)
        return NaturalIntakeGateResult(handled=True)
    if intake.action in {"reply_without_agent", "error_without_agent"}:
        send_result = await adapter.send(chat_id, intake.reply_text, reply_to=message_id)
        register_delivered_prompt(adapter._intake_prompt_msgs, chat_id, intake, send_result)
        return NaturalIntakeGateResult(handled=True)
    if intake.action == "bound_source":
        return NaturalIntakeGateResult(
            handled=False,
            source=intake.source,
            text=intake.text,
            control_consumed=bool(getattr(intake, "control_consumed", False)),
        )
    return NaturalIntakeGateResult(handled=False, source=source, text=text)
