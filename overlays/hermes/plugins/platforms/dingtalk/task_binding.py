"""Explicit DingTalk prefix binding to one persistent Kanban task."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass


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
    )
