"""DingTalk task binding: explicit ``#任务`` prefix parsing plus the natural
task intake seam (quote-aware confirmation R9 #3 hardened by R2 C1/C2 + R3
#2/#3 — prompts registered and matched as full ``(operation_id, phase,
target_digest)`` triples with explicit quote authentication, malformed
``repliedMsg`` fail-closed, direct quote clarification, media binding
restore R9 #6, sender/inbound-msgId honest refusal R9 #9 + R2/R5 I1,
undelivered-prompt withdrawal R5 I2)."""

from __future__ import annotations

import asyncio
import contextvars
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


def validate_natural_intake_classifier_config(extra, adapter_name):
    """Startup fail-closed probe for the intent classifier config (R2 C3).

    Runs only when ``natural_task_intake`` is on, mirroring Core's call-time
    check ``validate_intent_classifier_config(read_intent_classification_config())``
    so a missing/malformed ``auxiliary.intent_classification`` surfaces at
    connect time as a CRITICAL, ops-visible log. The feature is NOT silently
    disabled and startup never aborts — Core still fails honestly per
    message; this probe only makes the misconfiguration visible early. An
    old Core without the helpers is skipped gracefully.
    """
    if (extra or {}).get("natural_task_intake") is not True:
        return
    try:
        from gateway.intent_classifier import (
            read_intent_classification_config,
            validate_intent_classifier_config,
        )
    except Exception:  # noqa: BLE001 - old Core without R2 C3 helpers: skip
        return
    try:
        validate_intent_classifier_config(read_intent_classification_config())
    except Exception:  # noqa: BLE001 - probe must never break startup
        logger.critical(
            "[%s] natural_task_intake is on but auxiliary.intent_classification "
            "is missing or malformed; natural intake will fail honestly per "
            "message until it is fixed",
            adapter_name,
            exc_info=True,
        )


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
# per-chat FIFO cap keeps a busy group from growing the map without bound,
# and R3 #7 adds a lazy global expiry sweep (run from register/match) plus a
# cap on the number of outer chat keys so idle chats can't accumulate
# without bound either. In-memory only (accepted residual R5): a restart
# degrades a quoted confirm to the T1 reply clarification — the safe
# direction, never a wrong consume.
_INTAKE_PROMPT_REGISTRY_TTL_SECONDS = 1200.0
_INTAKE_PROMPT_REGISTRY_MAX_PER_CHAT = 8
_INTAKE_PROMPT_REGISTRY_MAX_CHATS = 256


def register_intake_prompt(
    registry, chat_id, message_id, operation_id, phase, target_digest, *, now=None
):
    """Record that outbound ``message_id`` carried the confirmation prompt for
    the ``(operation_id, phase, target_digest)`` triple (expiry in monotonic
    seconds)."""
    if not chat_id or not message_id or not operation_id:
        return
    now = time.monotonic() if now is None else now
    _sweep_expired_intake_prompts(registry, now)
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
    while len(registry) > _INTAKE_PROMPT_REGISTRY_MAX_CHATS:
        evicted_chat = next(iter(registry))
        registry.pop(evicted_chat, None)
        logger.warning(
            "Intake prompt registry chat count exceeds %d; evicted oldest chat has_chat_id=%s",
            _INTAKE_PROMPT_REGISTRY_MAX_CHATS,
            bool(evicted_chat),
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


def _sweep_expired_intake_prompts(registry, now):
    """Global lazy expiry sweep across every chat (R3 #7).

    Runs from register/match so entries of chats that never see another
    prompt or quote are still reclaimed, and their emptied outer keys drop.
    """
    for chat_id in list(registry):
        _purge_expired_intake_prompts(registry, chat_id, now)


def match_intake_prompt(registry, chat_id, message_id, *, now=None):
    """Return the ``{"operation_id", "phase", "target_digest"}`` triple whose
    confirmation prompt was ``message_id``, or None when the quoted message
    was never registered (or has expired)."""
    now = time.monotonic() if now is None else now
    # R4/R5 M5: sweep globally on EVERY call — even a degenerate one with
    # empty parameters — before the empty-check and any lookup, so expired
    # entries of any chat are always reclaimed.
    _sweep_expired_intake_prompts(registry, now)
    if not chat_id or not message_id:
        return None
    entries = registry.get(chat_id) or {}
    hit = entries.get(message_id)
    if hit is None:
        return None
    operation_id, phase, target_digest, expires_at = hit
    if expires_at <= now:  # defensive: the sweep above already purged it
        return None
    return {
        "operation_id": operation_id,
        "phase": phase,
        "target_digest": target_digest,
    }


def register_delivered_prompt(registry, chat_id, intake, send_result, *, now=None):
    """Register a confirmation prompt after it was actually delivered (D6).

    A ``reply_without_agent`` — or a ``quote_clarification`` restating a
    live pending's prompt (R3 #3) — is registered only when it carries a
    complete prompt triple (``prompt_operation_id`` + ``prompt_phase`` +
    ``prompt_target_digest``) and its send succeeded with a real outbound
    message id; anything else (errors, legacy results, failed sends,
    incomplete triples, a no-pending clarification) is a no-op — a quote of
    that message then simply stays unauthenticated.
    """
    if getattr(intake, "action", None) not in {
        "reply_without_agent",
        QUOTE_CLARIFICATION_ACTION,
    }:
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


async def discard_undelivered_intake_pending(adapter: object, source: object, intake: object):
    """Withdraw the pending after its confirmation prompt failed to deliver (R5 I2).

    Calls Core's ``discard_natural_intake_pending`` with the intake result's
    full prompt triple — Core CAS-clears the pending only when the live
    triple still matches, zero side effects otherwise — so a blind "确认"
    can't consume a prompt the user never saw. A False return or any failure
    (including an old Core without the helper) degrades to a warning: the
    pending may linger undelivered. Offloaded like the resolver (SQLite CAS).
    """
    operation_id = getattr(intake, "prompt_operation_id", None)
    phase = getattr(intake, "prompt_phase", None)
    target_digest = getattr(intake, "prompt_target_digest", None)
    if not operation_id or not phase or not target_digest:
        return  # no pending triple on this result — nothing to withdraw
    name = getattr(adapter, "name", "dingtalk")
    try:
        from gateway.task_intake import discard_natural_intake_pending
    except Exception:  # noqa: BLE001 - old Core without the R5 I2 helper
        logger.warning(
            "[%s] Prompt delivery failed but this Core cannot discard the pending; it may linger undelivered",
            name,
        )
        return
    try:
        discarded = await asyncio.to_thread(
            discard_natural_intake_pending,
            adapter._session_store,
            source,
            operation_id=operation_id,
            phase=phase,
            target_digest=target_digest,
        )
    except Exception:  # noqa: BLE001 - the send already failed; never crash the gate
        logger.warning(
            "[%s] Failed to discard the undelivered intake pending; it may linger undelivered",
            name,
            exc_info=True,
        )
        return
    if not discarded:
        logger.warning(
            "[%s] Undelivered intake pending was not discarded (already changed or gone); it may linger undelivered",
            name,
        )


def build_intake_quote(registry, chat_id, message):
    """Assemble the quote payload for Core, or None for a non-quote message.

    Every quote-reply yields a payload with an explicit authentication
    verdict (R2 C1/C2, R3 #2):

    * ``authenticated=True`` and ``matched`` = the registered
      ``{"operation_id", "phase", "target_digest"}`` triple — only when the
      quoted message is a registered, unexpired confirmation prompt of this
      chat.
    * ``authenticated=False`` and ``matched=None`` for everything else:
      unknown/expired/another chat's message, a ``repliedMsg`` without
      ``msgId``, a malformed ``repliedMsg`` (non-dict, empty, or null — the
      key's mere presence makes the message a quote; R3 #2 forbids silently
      degrading it to "not a quote"), or a webhook-delivered prompt (its
      ``SendResult.message_id`` is a locally synthesized uuid DingTalk never
      echoes back, so it can never match). ``quoted_text`` still rides along
      for Core to display — it never authenticates anything.
    """
    try:
        from .reply_context import _extract_replied_text_original, _get_text_extensions
    except ImportError:  # standalone (non-package) module load
        from reply_context import _extract_replied_text_original, _get_text_extensions  # type: ignore

    extensions = _get_text_extensions(message) or {}
    if "repliedMsg" not in extensions:
        return None
    replied = extensions.get("repliedMsg")
    replied_map = replied if isinstance(replied, dict) else {}
    replied_message_id = str(replied_map.get("msgId") or replied_map.get("msgid") or "").strip()
    matched = None
    if replied_message_id:
        matched = match_intake_prompt(registry, chat_id, replied_message_id)
    return {
        "replied_message_id": replied_message_id or None,
        "authenticated": matched is not None,
        "matched": matched,
        "quoted_text": _extract_replied_text_original(replied_map) or None,
    }


@dataclass(frozen=True)
class NaturalIntakeGateResult:
    """Outcome of the natural-intake gate for one inbound message."""

    handled: bool  # True => the adapter must stop (a reply was sent / error)
    source: object = None
    text: str = ""
    control_consumed: bool = False
    # D2: quoted-original passthrough for the bound first turn after a
    # consumed confirmation (display-only — NEVER an authorization signal).
    quoted_text: object = None
    quote_authenticated: bool = False
    replied_message_id: object = None


# R3 #3: Core's action for "a quote is present but unauthenticated — ask for
# clarification" (name confirmed by Core R3). Kit replies directly and never
# lets the message reach the agent, instead of relying on the reply-context
# sentinel. With a live pending Core restates its prompt and attaches the
# full prompt triple, so the clarification itself is registrable
# (``register_delivered_prompt``) — quoting it then authenticates like
# quoting any fresh prompt.
QUOTE_CLARIFICATION_ACTION = "quote_clarification"


def gate_bound_reply_kwargs(gate: NaturalIntakeGateResult) -> dict:
    """D2.3: reply-context kwargs for the bound first turn after a consumed
    confirmation.  The gate already extracted the quoted original (zero
    re-parse); a quote of the bot's own registered proposal marks
    ``reply_to_is_own_message``.  ``quoted_text`` empty (DingTalk delivered
    no original) → ``{}`` — the bound first turn NEVER triggers the T1
    clarification (confirming must not be answered with "what are you
    quoting?").  ``quoted_text`` is display-only, never an authorization
    signal."""
    if not gate.quoted_text:
        return {}
    return {
        "reply_to_message_id": gate.replied_message_id,
        "reply_to_text": gate.quoted_text,
        "reply_to_is_own_message": bool(gate.quote_authenticated),
    }


# R5 I1: honest user-facing error when an intake-eligible text message
# lacks the critical identifiers (stable sender / inbound msgId) — the gate
# sends this and stops instead of letting the message reach the main agent.
_MISSING_IDENTITY_REPLY = "消息缺少稳定身份/编号，无法安全处理任务。"

# R6 M4: per-(chat, reason) cooldown for the refusal receipt. A storm of
# malformed messages (each msgId-less callback arrives with a fresh dedup
# UUID, invisible to dedup) must not flood the chat — inside the window the
# message is still handled (zero state writes, zero agent delivery) but
# nothing is re-sent. In-memory and lazily expired on access, same
# lifecycle as the prompt registry.
_INTAKE_REFUSAL_COOLDOWN_SECONDS = 300.0
_INTAKE_REFUSAL_COOLDOWN_MAX_KEYS = 256


async def _send_intake_refusal(adapter: object, chat_id: str, message_id: str, reason: str):
    """Send the R5 I1 refusal receipt at most once per (chat, reason) window.

    R6 M4: cooled-down repeats return silently (the caller still ends the
    message handled). R6 I2: a failed or raising send only logs — the stamp
    is NOT set, so the next malformed message retries the receipt; there is
    never a second send attempt for the same message.
    """
    now = time.monotonic()
    stamps = getattr(adapter, "_intake_refusal_stamps", None)
    if stamps is None:
        stamps = adapter._intake_refusal_stamps = {}
    for key in [k for k, sent_at in stamps.items() if now - sent_at >= _INTAKE_REFUSAL_COOLDOWN_SECONDS]:
        stamps.pop(key, None)
    key = (chat_id, reason)
    if key in stamps:
        return
    name = getattr(adapter, "name", "dingtalk")
    try:
        send_result = await adapter.send(chat_id, _MISSING_IDENTITY_REPLY, reply_to=message_id)
    except Exception:  # noqa: BLE001 - receipt failure must never break the gate
        logger.warning(
            "[%s] Intake refusal receipt send raised (reason=%s)", name, reason, exc_info=True
        )
        return
    if not getattr(send_result, "success", False):
        logger.warning(
            "[%s] Intake refusal receipt was not delivered (reason=%s)", name, reason
        )
        return
    if len(stamps) >= _INTAKE_REFUSAL_COOLDOWN_MAX_KEYS:
        stamps.pop(next(iter(stamps)), None)
    stamps[key] = now


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
    * R9 #9 (D10) + R5 I1: an intake-eligible text without a stable sender
      id fails closed AND loud — the gate sends an honest error and stops;
      the message never reaches the tool-wielding main agent. R6 M4: the
      receipt is cooled down per (chat, reason); R6 I2: a failed/raising
      receipt send only logs.
    * R2 + R5 I1: same for a missing platform-stable inbound message id — a
      random UUID must never serve as the intake request number, so the
      resolver is skipped (no pending created or consumed) and the user
      gets the honest error instead of a silent agent dispatch.
    * R5 I2 + R6 I2: when delivering a confirmation/clarification prompt
      fails OR the send raises, the pending it points at is withdrawn via
      Core's ``discard_natural_intake_pending`` so a blind confirm can't
      consume an unseen prompt; registration only happens on successful
      delivery and the exception never escapes the gate.
    * R9 #3 (D6) + R2 C1/C2: a quote-reply is resolved through
      ``build_intake_quote``; Core decides whether a control intent may
      consume that operation. A delivered confirmation prompt is registered
      by its outbound message id together with the full prompt triple
      (``register_delivered_prompt``) so a later quote can only authenticate
      the exact phase it replied to.
    * R3 #3: Core's quote-clarification action (an unauthenticated quote on
      a control intent) is answered directly here and never reaches the
      agent — no reliance on the reply-context sentinel (see
      ``QUOTE_CLARIFICATION_ACTION``).

    ``handled=True`` means a reply was already sent (or the resolver failed
    after the user was told) and the adapter must return. Otherwise the
    (possibly re-bound) source, the text to dispatch, and whether a control
    message was consumed are handed back — a consumed control makes the
    adapter drop the reply context so the T1 clarification stays silent.
    """
    # H1 C1: publish this turn's dispatch scope for the delivery record
    # points — every message reaching the gate is a potential proposal turn.
    set_h1_dispatch_scope(source=source, text=text, message_id=message_id)
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
        # R9 #9 (D10) + R5 I1: fail closed AND fail loud — an intake-eligible
        # text message without a stable sender identity gets an honest error
        # and stops; it must NOT fall through to the tool-wielding main
        # agent, where it would bypass the intake confirmation/CAS guards.
        # R6 M4: the receipt itself is cooled down per chat+reason.
        logger.warning(
            "[%s] Natural task intake refused: no stable sender identity",
            getattr(adapter, "name", "dingtalk"),
        )
        await _send_intake_refusal(adapter, chat_id, message_id, "no_sender")
        return NaturalIntakeGateResult(handled=True)
    if not has_stable_message_id(message):
        # R2 + R5 I1: same contract — without a platform-stable inbound msgId
        # the intake request number would have to be the random UUID
        # ``_on_message`` synthesizes for dedup, so no pending may be created
        # or consumed; the user gets the honest error and the message stops.
        logger.warning(
            "[%s] Natural task intake refused: no stable inbound message id",
            getattr(adapter, "name", "dingtalk"),
        )
        await _send_intake_refusal(adapter, chat_id, message_id, "no_message_id")
        return NaturalIntakeGateResult(handled=True)
    quote = build_intake_quote(adapter._intake_prompt_msgs, chat_id, message)
    try:
        intake = await resolve_natural_intake(adapter, source, text or "", message_id, quote=quote)
        if intake.action not in {
            "pass_through",
            "reply_without_agent",
            "bound_source",
            "error_without_agent",
            QUOTE_CLARIFICATION_ACTION,
        }:
            raise ValueError(f"Unknown natural task intake action: {intake.action!r}")
    except Exception:
        logger.exception("[%s] Natural task intake failed", getattr(adapter, "name", "dingtalk"))
        await adapter.send(chat_id, "任务接入暂时不可用，请稍后重试。", reply_to=message_id)
        return NaturalIntakeGateResult(handled=True)
    if intake.action in {"reply_without_agent", "error_without_agent", QUOTE_CLARIFICATION_ACTION}:
        try:
            send_result = await adapter.send(chat_id, intake.reply_text, reply_to=message_id)
        except Exception:
            # R6 I2: a raising send is a failed delivery too — withdraw the
            # pending below and end handled; never let the exception escape
            # into _safe_on_message with the pending alive.
            logger.warning(
                "[%s] Intake prompt send raised; treating as undelivered",
                getattr(adapter, "name", "dingtalk"),
                exc_info=True,
            )
            send_result = None
        if getattr(send_result, "success", False):
            register_delivered_prompt(adapter._intake_prompt_msgs, chat_id, intake, send_result)
        else:
            # R5 I2: the prompt never reached the user — withdraw the pending
            # it points at so a blind "确认" can't consume an unseen prompt.
            await discard_undelivered_intake_pending(adapter, source, intake)
        return NaturalIntakeGateResult(handled=True)
    if intake.action == "bound_source":
        return NaturalIntakeGateResult(
            handled=False,
            source=intake.source,
            text=intake.text,
            control_consumed=bool(getattr(intake, "control_consumed", False)),
            # D2: pass the already-extracted quote payload through so the
            # bound first turn after a consumed confirmation keeps the
            # quoted original (zero re-parse; display-only, never auth).
            quoted_text=(quote or {}).get("quoted_text") if isinstance(quote, dict) else None,
            quote_authenticated=bool((quote or {}).get("authenticated")) if isinstance(quote, dict) else False,
            replied_message_id=(quote or {}).get("replied_message_id") if isinstance(quote, dict) else None,
        )
    return NaturalIntakeGateResult(handled=False, source=source, text=text)


# ---------------------------------------------------------------------------
# H1 intake proposal seam (C1): one-shot slot + post-delivery install.
#
# The h1_intake_propose tool (plugins/h1_intake_proposal) only validates and
# slots a proposal (zero Kanban writes, zero pending installs).  The pending
# installs ONLY after the turn's NON-RECEIPT final reply is provably
# delivered (C1) — outcome==SUCCESS AND a generation-matched delivery record
# — and is registered for quote authentication atomically in the same
# callback.  Delivery failure / exception turn / sanitized failure receipt
# (R3) / state drift => the slot is dropped with a warning; nothing was ever
# installed, so nothing needs withdrawal.
#
# All mutable state lives on the adapter instance (lazy attrs), so any
# loaded instance of this module drives the same coherent behavior.
# ---------------------------------------------------------------------------

# Same TTL as Core's AWAITING_TTL_SECONDS — a slot must never outlive the
# pending it points at.
_H1_PROPOSAL_SLOT_TTL_SECONDS = 900.0
_H1_PROPOSAL_MAX_KEYS = 256

# Per-turn dispatch scope, published by the adapter's ``_on_message`` right
# before dispatch (ContextVar: the background processing task inherits a
# copy at creation, so the delivery record points see exactly this turn's
# source/text — generation-correlated, never "chat latest outbound", I3).
_h1_dispatch_scope: contextvars.ContextVar = contextvars.ContextVar(
    "h1_intake_dispatch_scope", default=None
)


def set_h1_dispatch_scope(*, source, text, message_id):
    _h1_dispatch_scope.set(
        {"source": source, "text": text, "message_id": message_id}
    )


def h1_dispatch_scope():
    return _h1_dispatch_scope.get()


def session_key_for_h1(adapter, source):
    """Session key for the proposal slot — derived exactly like base.py's."""
    from gateway.session import build_session_key

    extra = getattr(getattr(adapter, "config", None), "extra", None) or {}
    return build_session_key(
        source,
        group_sessions_per_user=extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
    )


def current_h1_generation(adapter, session_key):
    """The gateway run generation bound to the session's active interrupt
    event (run.py ``_bind_adapter_run_generation``), or None when unknown."""
    active = getattr(adapter, "_active_sessions", None) or {}
    event = active.get(session_key)
    return getattr(event, "_hermes_run_generation", None) if event is not None else None


def slot_h1_proposal(adapter, session_key, *, pending, source, generation):
    """One-shot proposal slot.  Returns ``None`` when the slot was taken,
    ``"already_slotted"`` for an idempotent repeat of the SAME operation
    (message redelivery, M5), or ``"slot_busy"`` when a different proposal
    is already slotted for this session (one proposal per turn)."""
    if not session_key:
        return "slot_busy"
    slots = getattr(adapter, "_h1_proposal_slots", None)
    if slots is None:
        slots = adapter._h1_proposal_slots = {}
    now = time.monotonic()
    for key in [k for k, s in slots.items() if s["expires_at"] <= now]:
        slots.pop(key, None)
    existing = slots.get(session_key)
    if existing is not None:
        if existing["pending"].get("operation_id") == pending.get("operation_id"):
            return "already_slotted"
        return "slot_busy"
    if len(slots) >= _H1_PROPOSAL_MAX_KEYS:
        slots.pop(next(iter(slots)), None)
    slots[session_key] = {
        "pending": pending,
        "source": source,
        "session_key": session_key,
        "generation": generation,
        "expires_at": now + _H1_PROPOSAL_SLOT_TTL_SECONDS,
    }
    return None


def pop_h1_proposal_slot(adapter, session_key):
    slots = getattr(adapter, "_h1_proposal_slots", None)
    if not slots:
        return None
    return slots.pop(session_key, None)


def record_h1_run_outcome(adapter, session_key, generation, outcome):
    """Capture the run outcome for the post-delivery install gate."""
    if not session_key or generation is None:
        return
    outcomes = getattr(adapter, "_h1_run_outcomes", None)
    if outcomes is None:
        outcomes = adapter._h1_run_outcomes = {}
    if len(outcomes) >= _H1_PROPOSAL_MAX_KEYS:
        outcomes.pop(next(iter(outcomes)), None)
    outcomes[(session_key, int(generation))] = outcome


def record_h1_final_reply_delivery(adapter, session_key, generation, message_id):
    """Record the delivered final reply id for (session, generation)."""
    if not session_key or generation is None or not message_id:
        return
    records = getattr(adapter, "_h1_final_reply_records", None)
    if records is None:
        records = adapter._h1_final_reply_records = {}
    if len(records) >= _H1_PROPOSAL_MAX_KEYS:
        records.pop(next(iter(records)), None)
    records[(session_key, int(generation))] = message_id


def capture_h1_run_outcome(adapter, event, outcome):
    """on_processing_complete seam: capture the outcome against the run's
    (session_key, generation) — the install gate's SUCCESS evidence."""
    source = getattr(event, "source", None)
    if source is None:
        return
    try:
        session_key = session_key_for_h1(adapter, source)
    except Exception:
        return
    generation = current_h1_generation(adapter, session_key)
    record_h1_run_outcome(adapter, session_key, generation, outcome)


def _failure_receipt_marked() -> bool:
    """True when the turn's outgoing final reply was stamped as a failure
    receipt at its production point (Core gateway.honest_failure, R3).

    An old Core without the helper cannot tell a sanitized provider-error
    receipt from the agent's own words — fail closed (treat as marked):
    the record is skipped, the proposal simply never installs.
    """
    try:
        from gateway.honest_failure import is_failure_receipt

        return bool(is_failure_receipt())
    except Exception:  # noqa: BLE001 - no mark readable => fail closed
        return True


def record_h1_final_reply(adapter, message_id):
    """Delivery record point for the turn's final reply (card finalize /
    final card send success).  Records ONLY when the turn's dispatch scope
    is known and the reply is NOT a stamped failure receipt (R3)."""
    if not message_id:
        return
    if _failure_receipt_marked():
        logger.debug(
            "[%s] H1 final-reply record skipped: failure receipt",
            getattr(adapter, "name", "dingtalk"),
        )
        return
    scope = _h1_dispatch_scope.get()
    if not scope or scope.get("source") is None:
        return
    try:
        session_key = session_key_for_h1(adapter, scope["source"])
    except Exception:
        return
    generation = current_h1_generation(adapter, session_key)
    record_h1_final_reply_delivery(adapter, session_key, generation, message_id)


def _outcome_is_success(outcome) -> bool:
    return getattr(outcome, "name", outcome) == "SUCCESS"


async def install_h1_proposal_after_delivery(adapter, session_key):
    """C1 post-delivery gate — the ONLY pending install point for proposals.

    Atomic three steps behind one check: ① the turn ended outcome==SUCCESS
    AND a generation-matched NON-RECEIPT final-reply delivery record exists
    (R3: failure receipts never record); ② re-read state (drift recheck)
    and ``_cas_install_prompt`` the slotted pending; ③ register the prompt
    triple against the delivered final reply's message id.  Any failure
    drops the slot with a warning — nothing was installed, nothing to
    withdraw (C1).
    """
    slot = pop_h1_proposal_slot(adapter, session_key)
    if slot is None:
        return
    name = getattr(adapter, "name", "dingtalk")
    generation = slot["generation"]
    outcome = (getattr(adapter, "_h1_run_outcomes", None) or {}).get(
        (session_key, generation)
    )
    delivered_id = (getattr(adapter, "_h1_final_reply_records", None) or {}).get(
        (session_key, generation)
    )
    if not _outcome_is_success(outcome) or not delivered_id:
        logger.warning(
            "[%s] H1 intake proposal dropped: delivery gate unmet "
            "(outcome=%r, final_reply_recorded=%s); nothing was installed",
            name,
            outcome,
            bool(delivered_id),
        )
        return
    pending = slot["pending"]
    source = slot["source"]
    try:
        from gateway.task_intake import (
            _awaiting_is_live,
            _cas_install_prompt,
            _state_copy,
        )
    except Exception:  # noqa: BLE001 - old Core without the helpers
        logger.warning(
            "[%s] H1 intake proposal dropped: Core install helpers unavailable",
            name,
        )
        return

    def _install():
        state = _state_copy(adapter._session_store.get_task_state(source))
        existing = state.get("pending_confirmation")
        if isinstance(existing, dict) and (
            existing.get("state") == "applying"
            or _awaiting_is_live(existing, time.time())
        ):
            return "drift"
        replacement, install_error = _cas_install_prompt(
            adapter._session_store,
            source,
            state,
            pending,
            str(pending.get("original_text") or ""),
        )
        if install_error is not None or replacement is None:
            return None
        return replacement

    try:
        installed = await asyncio.to_thread(_install)
    except Exception:  # noqa: BLE001 - the delivery already happened; never crash
        logger.warning(
            "[%s] H1 intake proposal install raised; nothing was installed",
            name,
            exc_info=True,
        )
        return
    if installed is None or installed == "drift":
        logger.warning(
            "[%s] H1 intake proposal not installed (state drift or CAS "
            "failure); nothing was installed",
            name,
        )
        return
    register_intake_prompt(
        adapter._intake_prompt_msgs,
        getattr(source, "chat_id", None),
        delivered_id,
        pending.get("operation_id"),
        pending.get("phase"),
        pending.get("target_digest"),
    )
