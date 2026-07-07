#!/usr/bin/env python3
"""Apply the Hermes DingTalk compatibility patch set.

The patcher intentionally uses small, anchored source edits instead of copying
whole gateway files. Each edit is idempotent: if its marker is already present,
the edit is skipped; otherwise the original anchor must match exactly.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


Mode = Literal["check", "apply", "verify"]


REPLY_SENTINEL = '_REPLY_ORIGINAL_UNAVAILABLE = "\\x00__HERMES_REPLY_ORIGINAL_UNAVAILABLE__\\x00"'


@dataclass(frozen=True)
class Step:
    name: str
    path: str
    marker: str | tuple[str, ...]
    old: str | tuple[str, ...]
    new: str | tuple[str, ...]

    def markers(self) -> tuple[str, ...]:
        if isinstance(self.marker, str):
            return (self.marker,)
        return self.marker

    def replacements(self) -> tuple[tuple[str, str], ...]:
        old_values = (self.old,) if isinstance(self.old, str) else self.old
        new_values = (self.new,) if isinstance(self.new, str) else self.new
        if len(old_values) != len(new_values):
            raise ValueError(f"{self.name} replacement variants are unbalanced")
        return tuple(zip(old_values, new_values))


@dataclass
class StepResult:
    name: str
    path: str
    status: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {"name": self.name, "path": self.path, "status": self.status}
        if self.message:
            data["message"] = self.message
        return data


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_present(text: str, markers: Iterable[str]) -> bool:
    return all(marker in text for marker in markers)


RUN_STEPS = [
    Step(
        name="run.reply_sentinel_constant",
        path="gateway/run.py",
        marker=REPLY_SENTINEL,
        old=(
            "_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h\n"
            "_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0\n"
            "_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT = 5.0\n"
            '_TELEGRAM_COMMAND_MENTION_RE = re.compile(r"(?<![\\w:/])/([A-Za-z0-9][A-Za-z0-9_-]*)")\n'
        ),
        new=(
            "_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h\n"
            "_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0\n"
            "_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT = 5.0\n"
            "# T27: sentinel emitted by the DingTalk adapter when the user replied to an earlier\n"
            "# message but the platform did not deliver the quoted original text. Value MUST match\n"
            "# the adapter's _REPLY_ORIGINAL_UNAVAILABLE exactly; the reply-injection block below\n"
            "# keys off it to emit a Chinese back-reference instruction instead of a quote template.\n"
            f"{REPLY_SENTINEL}\n"
            '_TELEGRAM_COMMAND_MENTION_RE = re.compile(r"(?<![\\w:/])/([A-Za-z0-9][A-Za-z0-9_-]*)")\n'
        ),
    ),
    Step(
        name="run.dingtalk_home_prompt_gate",
        path="gateway/run.py",
        marker="def _should_prompt_for_home_channel(source: Any) -> bool:",
        old=(
            "def _home_target_env_var(platform_name: str) -> str:\n"
            "    \"\"\"Return the configured home-target env var for a platform.\n"
            "\n"
            "    Consults built-in ``_HOME_TARGET_ENV_VARS`` first, then the plugin\n"
            "    registry via ``cron.scheduler._resolve_home_env_var``, then falls back\n"
            "    to ``<PLATFORM>_HOME_CHANNEL`` for unknown names.\n"
            "    \"\"\"\n"
            "    from cron.scheduler import _resolve_home_env_var\n"
            "\n"
            "    resolved = _resolve_home_env_var(platform_name)\n"
            "    if resolved:\n"
            "        return resolved\n"
            "    return f\"{platform_name.upper()}_HOME_CHANNEL\"\n"
            "\n"
            "\n"
            "def _home_thread_env_var(platform_name: str) -> str:\n"
        ),
        new=(
            "def _home_target_env_var(platform_name: str) -> str:\n"
            "    \"\"\"Return the configured home-target env var for a platform.\n"
            "\n"
            "    Consults built-in ``_HOME_TARGET_ENV_VARS`` first, then the plugin\n"
            "    registry via ``cron.scheduler._resolve_home_env_var``, then falls back\n"
            "    to ``<PLATFORM>_HOME_CHANNEL`` for unknown names.\n"
            "    \"\"\"\n"
            "    from cron.scheduler import _resolve_home_env_var\n"
            "\n"
            "    resolved = _resolve_home_env_var(platform_name)\n"
            "    if resolved:\n"
            "        return resolved\n"
            "    return f\"{platform_name.upper()}_HOME_CHANNEL\"\n"
            "\n"
            "\n"
            "def _should_prompt_for_home_channel(source: Any) -> bool:\n"
            "    platform = getattr(source, \"platform\", None)\n"
            "    if not platform or platform in {Platform.LOCAL, Platform.WEBHOOK}:\n"
            "        return False\n"
            "    if platform == Platform.DINGTALK and getattr(source, \"chat_type\", \"\") == \"group\":\n"
            "        return False\n"
            "    return True\n"
            "\n"
            "\n"
            "def _home_thread_env_var(platform_name: str) -> str:\n"
        ),
    ),
    Step(
        name="run.reply_context_sentinel_branch",
        path="gateway/run.py",
        marker="event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE",
        old=(
            "        if getattr(event, \"reply_to_text\", None) and event.reply_to_message_id:\n"
            "            # Always inject the reply-to pointer — even when the quoted text\n"
            "            # already appears in history. The prefix isn't deduplication, it's\n"
            "            # disambiguation: it tells the agent *which* prior message the user\n"
            "            # is referencing. History can contain the same or similar text\n"
            "            # multiple times, and without an explicit pointer the agent has to\n"
            "            # guess (or answer for both subjects). Token overhead is minimal.\n"
            "            reply_snippet = event.reply_to_text[:500]\n"
            "            if getattr(event, \"reply_to_is_own_message\", False):\n"
            "                message_text = (\n"
            "                    f'[Replying to your previous message: \"{reply_snippet}\"]\\n\\n'\n"
            "                    f\"{message_text}\"\n"
            "                )\n"
            "            else:\n"
            "                message_text = f'[Replying to: \"{reply_snippet}\"]\\n\\n{message_text}'\n"
        ),
        new=(
            "        if getattr(event, \"reply_to_text\", None) and event.reply_to_message_id:\n"
            "            # Always inject the reply-to pointer — even when the quoted text\n"
            "            # already appears in history. The prefix isn't deduplication, it's\n"
            "            # disambiguation: it tells the agent *which* prior message the user\n"
            "            # is referencing. History can contain the same or similar text\n"
            "            # multiple times, and without an explicit pointer the agent has to\n"
            "            # guess (or answer for both subjects). Token overhead is minimal.\n"
            "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
            "                # T27: user quoted an earlier message but the platform (DingTalk) did\n"
            "                # not deliver the original text. We can't render a quote snippet, so\n"
            "                # instruct the agent to resolve the reference from conversation history\n"
            "                # rather than assuming it points at the most-recent topic.\n"
            "                message_text = (\n"
            "                    \"【系统提示】用户正在引用本会话中更早的一条消息向你提问，但本次未取到被引用消息的原文。\"\n"
            "                    \"请先回顾上文对话历史，找到用户引用的那条具体消息，据此判断用户此处“这个/这个问题/它”\"\n"
            "                    \"等指代的真正对象，再作答；不要想当然地认为指的是最近讨论的话题。\\n\\n\"\n"
            "                    f\"{message_text}\"\n"
            "                )\n"
            "            else:\n"
            "                reply_snippet = event.reply_to_text[:500]\n"
            "                if getattr(event, \"reply_to_is_own_message\", False):\n"
            "                    message_text = (\n"
            "                        f'[Replying to your previous message: \"{reply_snippet}\"]\\n\\n'\n"
            "                        f\"{message_text}\"\n"
            "                    )\n"
            "                else:\n"
            "                    message_text = f'[Replying to: \"{reply_snippet}\"]\\n\\n{message_text}'\n"
        ),
    ),
    Step(
        name="run.home_prompt_condition",
        path="gateway/run.py",
        marker="if not history and _should_prompt_for_home_channel(source):",
        old=(
            "        if not history and source.platform and source.platform != Platform.LOCAL and source.platform != Platform.WEBHOOK:\n"
        ),
        new=(
            "        if not history and _should_prompt_for_home_channel(source):\n"
        ),
    ),
    Step(
        name="run.session_env_fields",
        path="gateway/run.py",
        marker=(
            'chat_type=context.source.chat_type or "",',
            'user_id_alt=str(context.source.user_id_alt) if context.source.user_id_alt else "",',
            "session_id=context.session_id,",
        ),
        old=(
            (
                "        return set_session_vars(\n"
                "            platform=context.source.platform.value,\n"
                "            chat_id=context.source.chat_id,\n"
                "            chat_name=context.source.chat_name or \"\",\n"
                "            thread_id=str(context.source.thread_id) if context.source.thread_id else \"\",\n"
                "            user_id=str(context.source.user_id) if context.source.user_id else \"\",\n"
                "            user_name=str(context.source.user_name) if context.source.user_name else \"\",\n"
                "            session_key=context.session_key,\n"
                "            message_id=str(context.source.message_id) if context.source.message_id else \"\",\n"
                "            async_delivery=_async_delivery,\n"
                "        )\n"
            ),
            (
                "        return set_session_vars(\n"
                "            platform=context.source.platform.value,\n"
                "            chat_id=context.source.chat_id,\n"
                "            chat_name=context.source.chat_name or \"\",\n"
                "            thread_id=str(context.source.thread_id) if context.source.thread_id else \"\",\n"
                "            user_id=str(context.source.user_id) if context.source.user_id else \"\",\n"
                "            user_name=str(context.source.user_name) if context.source.user_name else \"\",\n"
                "            session_key=context.session_key,\n"
                "            message_id=str(context.source.message_id) if context.source.message_id else \"\",\n"
                "            profile=getattr(context.source, \"profile\", \"\") or \"\",\n"
                "            async_delivery=_async_delivery,\n"
                "        )\n"
            ),
        ),
        new=(
            (
                "        return set_session_vars(\n"
                "            platform=context.source.platform.value,\n"
                "            chat_id=context.source.chat_id,\n"
                "            chat_name=context.source.chat_name or \"\",\n"
                "            chat_type=context.source.chat_type or \"\",\n"
                "            thread_id=str(context.source.thread_id) if context.source.thread_id else \"\",\n"
                "            user_id=str(context.source.user_id) if context.source.user_id else \"\",\n"
                "            user_id_alt=str(context.source.user_id_alt) if context.source.user_id_alt else \"\",\n"
                "            user_name=str(context.source.user_name) if context.source.user_name else \"\",\n"
                "            session_key=context.session_key,\n"
                "            # 2026-07-02 T26: 补传 session_id。此前缺参导致 contextvar HERMES_SESSION_ID\n"
                "            # 每轮被显式置空串，仅新建 AIAgent 那轮由 agent_init 填上；agent 缓存命中轮\n"
                "            # 插件读到空值（dingtalk_group_push T25 的自愈兜底可保留）。\n"
                "            session_id=context.session_id,\n"
                "            message_id=str(context.source.message_id) if context.source.message_id else \"\",\n"
                "            async_delivery=_async_delivery,\n"
                "        )\n"
            ),
            (
                "        return set_session_vars(\n"
                "            platform=context.source.platform.value,\n"
                "            chat_id=context.source.chat_id,\n"
                "            chat_name=context.source.chat_name or \"\",\n"
                "            chat_type=context.source.chat_type or \"\",\n"
                "            thread_id=str(context.source.thread_id) if context.source.thread_id else \"\",\n"
                "            user_id=str(context.source.user_id) if context.source.user_id else \"\",\n"
                "            user_id_alt=str(context.source.user_id_alt) if context.source.user_id_alt else \"\",\n"
                "            user_name=str(context.source.user_name) if context.source.user_name else \"\",\n"
                "            session_key=context.session_key,\n"
                "            # 2026-07-02 T26: 补传 session_id。此前缺参导致 contextvar HERMES_SESSION_ID\n"
                "            # 每轮被显式置空串，仅新建 AIAgent 那轮由 agent_init 填上；agent 缓存命中轮\n"
                "            # 插件读到空值（dingtalk_group_push T25 的自愈兜底可保留）。\n"
                "            session_id=context.session_id,\n"
                "            message_id=str(context.source.message_id) if context.source.message_id else \"\",\n"
                "            profile=getattr(context.source, \"profile\", \"\") or \"\",\n"
                "            async_delivery=_async_delivery,\n"
                "        )\n"
            ),
        ),
    ),
]


SESSION_STEPS = [
    Step(
        name="session.session_key_validator",
        path="gateway/session.py",
        marker="def _is_session_key_unsafe(value: object) -> bool:",
        old=(
            "def _is_path_unsafe(value: object) -> bool:\n"
            "    \"\"\"Return True if ``value`` could traverse outside the sessions dir.\"\"\"\n"
            "    if not value:\n"
            "        return False\n"
            "    s = str(value)\n"
            "    if \"..\" in s or \"/\" in s or \"\\\\\" in s:\n"
            "        return True\n"
            "    # Leading Windows drive path, e.g. \"C:\\...\" or \"d:/...\". A bare \"x:\"\n"
            "    # with no following separator isn't a usable absolute path, and the\n"
            "    # separator forms are already caught above — but keep an explicit guard\n"
            "    # for the drive-letter prefix in case a separator was normalized away.\n"
            "    return len(s) >= 2 and s[0].isalpha() and s[1] == \":\"\n"
            "\n"
            "\n"
            "@dataclass\n"
        ),
        new=(
            "def _is_path_unsafe(value: object) -> bool:\n"
            "    \"\"\"Return True if ``value`` could traverse outside the sessions dir.\"\"\"\n"
            "    if not value:\n"
            "        return False\n"
            "    s = str(value)\n"
            "    if \"..\" in s or \"/\" in s or \"\\\\\" in s:\n"
            "        return True\n"
            "    # Leading Windows drive path, e.g. \"C:\\...\" or \"d:/...\". A bare \"x:\"\n"
            "    # with no following separator isn't a usable absolute path, and the\n"
            "    # separator forms are already caught above — but keep an explicit guard\n"
            "    # for the drive-letter prefix in case a separator was normalized away.\n"
            "    return len(s) >= 2 and s[0].isalpha() and s[1] == \":\"\n"
            "\n"
            "\n"
            "# session_key never flows into a filesystem path (only session_id does; see\n"
            "# hermes_state ``sessions_dir / f\"{session_id}.json\"``), but it does embed\n"
            "# platform-native chat ids. DingTalk conversation ids are base64 whose\n"
            "# alphabet includes ``/``, so the blanket ``/`` rejection above produces\n"
            "# false positives that silently drop those sessions on every reload. Keep\n"
            "# rejecting genuinely path-shaped values (parent traversal, backslashes,\n"
            "# absolute/home prefixes, drive letters) while allowing interior ``/``:\n"
            "# base64 cannot produce ``..``, so no traversal payload is admitted.\n"
            "def _is_session_key_unsafe(value: object) -> bool:\n"
            "    \"\"\"Return True if a session key looks like a path-escape attempt.\"\"\"\n"
            "    if not value:\n"
            "        return False\n"
            "    s = str(value)\n"
            "    if \"..\" in s or \"\\\\\" in s:\n"
            "        return True\n"
            "    if s.startswith((\"/\", \"~\")):\n"
            "        return True\n"
            "    return len(s) >= 2 and s[0].isalpha() and s[1] == \":\" and s[2:3] in (\"/\", \"\\\\\")\n"
            "\n"
            "\n"
            "@dataclass\n"
        ),
    ),
    Step(
        name="session.path_sensitive_validation",
        path="gateway/session.py",
        marker=(
            '("session_key", session_key, _is_session_key_unsafe)',
            '("session_id", session_id, _is_path_unsafe)',
        ),
        old=(
            "        # Validate path-sensitive fields to prevent directory traversal (CWE-22)\n"
            "        for _field, _val in ((\"session_key\", session_key), (\"session_id\", session_id)):\n"
            "            if _is_path_unsafe(_val):\n"
            "                raise ValueError(\n"
            "                    f\"Invalid {_field}: potential directory traversal detected\"\n"
            "                )\n"
        ),
        new=(
            "        # Validate path-sensitive fields to prevent directory traversal (CWE-22).\n"
            "        # session_id flows into filenames and keeps the strict check;\n"
            "        # session_key never does, and must tolerate base64 chat ids with \"/\".\n"
            "        for _field, _val, _checker in (\n"
            "            (\"session_key\", session_key, _is_session_key_unsafe),\n"
            "            (\"session_id\", session_id, _is_path_unsafe),\n"
            "        ):\n"
            "            if _checker(_val):\n"
            "                raise ValueError(\n"
            "                    f\"Invalid {_field}: potential directory traversal detected\"\n"
            "                )\n"
        ),
    ),
]


SESSION_CONTEXT_STEPS = [
    Step(
        name="session_context.contextvars",
        path="gateway/session_context.py",
        marker=(
            '_SESSION_CHAT_TYPE: ContextVar = ContextVar("HERMES_SESSION_CHAT_TYPE", default=_UNSET)',
            '_SESSION_USER_ID_ALT: ContextVar = ContextVar("HERMES_SESSION_USER_ID_ALT", default=_UNSET)',
        ),
        old=(
            '_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)\n'
            '_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)\n'
            '_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)\n'
            '_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)\n'
            '_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)\n'
        ),
        new=(
            '_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)\n'
            '_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)\n'
            '_SESSION_CHAT_TYPE: ContextVar = ContextVar("HERMES_SESSION_CHAT_TYPE", default=_UNSET)\n'
            '_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)\n'
            '_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)\n'
            '_SESSION_USER_ID_ALT: ContextVar = ContextVar("HERMES_SESSION_USER_ID_ALT", default=_UNSET)\n'
            '_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)\n'
        ),
    ),
    Step(
        name="session_context.var_map",
        path="gateway/session_context.py",
        marker=(
            '"HERMES_SESSION_CHAT_TYPE": _SESSION_CHAT_TYPE,',
            '"HERMES_SESSION_USER_ID_ALT": _SESSION_USER_ID_ALT,',
        ),
        old=(
            '    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,\n'
            '    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,\n'
            '    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,\n'
            '    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,\n'
            '    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,\n'
        ),
        new=(
            '    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,\n'
            '    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,\n'
            '    "HERMES_SESSION_CHAT_TYPE": _SESSION_CHAT_TYPE,\n'
            '    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,\n'
            '    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,\n'
            '    "HERMES_SESSION_USER_ID_ALT": _SESSION_USER_ID_ALT,\n'
            '    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,\n'
        ),
    ),
    Step(
        name="session_context.set_session_vars_signature",
        path="gateway/session_context.py",
        marker=(
            '    chat_type: str = "",',
            '    user_id_alt: str = "",',
        ),
        old=(
            "    chat_id: str = \"\",\n"
            "    chat_name: str = \"\",\n"
            "    thread_id: str = \"\",\n"
            "    user_id: str = \"\",\n"
            "    user_name: str = \"\",\n"
        ),
        new=(
            "    chat_id: str = \"\",\n"
            "    chat_name: str = \"\",\n"
            "    chat_type: str = \"\",\n"
            "    thread_id: str = \"\",\n"
            "    user_id: str = \"\",\n"
            "    user_id_alt: str = \"\",\n"
            "    user_name: str = \"\",\n"
        ),
    ),
    Step(
        name="session_context.set_session_vars_tokens",
        path="gateway/session_context.py",
        marker=(
            "_SESSION_CHAT_TYPE.set(chat_type),",
            "_SESSION_USER_ID_ALT.set(user_id_alt),",
        ),
        old=(
            "        _SESSION_CHAT_ID.set(chat_id),\n"
            "        _SESSION_CHAT_NAME.set(chat_name),\n"
            "        _SESSION_THREAD_ID.set(thread_id),\n"
            "        _SESSION_USER_ID.set(user_id),\n"
            "        _SESSION_USER_NAME.set(user_name),\n"
        ),
        new=(
            "        _SESSION_CHAT_ID.set(chat_id),\n"
            "        _SESSION_CHAT_NAME.set(chat_name),\n"
            "        _SESSION_CHAT_TYPE.set(chat_type),\n"
            "        _SESSION_THREAD_ID.set(thread_id),\n"
            "        _SESSION_USER_ID.set(user_id),\n"
            "        _SESSION_USER_ID_ALT.set(user_id_alt),\n"
            "        _SESSION_USER_NAME.set(user_name),\n"
        ),
    ),
    Step(
        name="session_context.clear_session_vars",
        path="gateway/session_context.py",
        marker=(
            "        _SESSION_CHAT_TYPE,\n",
            "        _SESSION_USER_ID_ALT,\n",
        ),
        old=(
            "        _SESSION_CHAT_ID,\n"
            "        _SESSION_CHAT_NAME,\n"
            "        _SESSION_THREAD_ID,\n"
            "        _SESSION_USER_ID,\n"
            "        _SESSION_USER_NAME,\n"
        ),
        new=(
            "        _SESSION_CHAT_ID,\n"
            "        _SESSION_CHAT_NAME,\n"
            "        _SESSION_CHAT_TYPE,\n"
            "        _SESSION_THREAD_ID,\n"
            "        _SESSION_USER_ID,\n"
            "        _SESSION_USER_ID_ALT,\n"
            "        _SESSION_USER_NAME,\n"
        ),
    ),
]


STEPS = [*RUN_STEPS, *SESSION_STEPS, *SESSION_CONTEXT_STEPS]
VERIFY_PATHS = ("gateway/run.py", "gateway/session.py", "gateway/session_context.py")
CANONICAL_SESSION_KEY_CHECKER = (
    "def _is_session_key_unsafe(value: object) -> bool:\n"
    "    \"\"\"Return True if a session key looks like a path-escape attempt.\"\"\"\n"
    "    if not value:\n"
    "        return False\n"
    "    s = str(value)\n"
    "    if \"..\" in s or \"\\\\\" in s:\n"
    "        return True\n"
    "    if s.startswith((\"/\", \"~\")):\n"
    "        return True\n"
    "    return len(s) >= 2 and s[0].isalpha() and s[1] == \":\" and s[2:3] in (\"/\", \"\\\\\")"
)


def _parse_python(path: Path) -> tuple[ast.Module | None, str]:
    try:
        return ast.parse(path.read_text(encoding="utf-8")), ""
    except SyntaxError as exc:
        return None, f"syntax error: {exc}"


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _import_binds_name(node: ast.Import | ast.ImportFrom, name: str) -> bool:
    for alias in node.names:
        bound_name = alias.asname or alias.name.split(".", 1)[0]
        if bound_name == name:
            return True
    return False


def _node_binds_name(node: ast.AST, name: str) -> bool:
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if child.name == name:
                return True
        elif isinstance(child, ast.Name):
            if child.id == name and isinstance(child.ctx, (ast.Store, ast.Del)):
                return True
        elif isinstance(child, ast.ExceptHandler):
            if child.name == name:
                return True
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)):
            if child.name == name:
                return True
        elif isinstance(child, ast.MatchMapping):
            if child.rest == name:
                return True
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            if _import_binds_name(child, name):
                return True
    return False


def _top_level_name_bindings(tree: ast.Module, name: str) -> list[ast.AST]:
    bindings: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                bindings.append(node)
        elif _node_binds_name(node, name):
            bindings.append(node)
    return bindings


def _has_dynamic_name_rebind_risk(tree: ast.Module, name: str) -> bool:
    risky_calls = {"exec", "eval", "globals", "locals", "vars"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if name in node.value:
                return True
        if isinstance(node, ast.Call) and _name(node.func) in risky_calls:
            return True
    return False


def _has_constant_assign(tree: ast.Module, target_name: str, value: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(_name(target) == target_name for target in node.targets):
                return isinstance(node.value, ast.Constant) and node.value.value == value
        if isinstance(node, ast.AnnAssign) and _name(node.target) == target_name:
            return isinstance(node.value, ast.Constant) and node.value.value == value
    return False


def _has_call_keyword(tree: ast.Module, call_name: str, keyword: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func).endswith(call_name):
            if any(item.arg == keyword for item in node.keywords):
                return True
    return False


def _has_function_arg(tree: ast.Module, function_name: str, arg_name: str) -> bool:
    func = _find_function(tree, function_name)
    if not func:
        return False
    return any(arg.arg == arg_name for arg in func.args.args)


def _has_contextvar_assign(tree: ast.Module, var_name: str, env_name: str) -> bool:
    for node in ast.walk(tree):
        target = None
        value = None
        if isinstance(node, ast.Assign):
            target = node.targets[0] if node.targets else None
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if _name(target) != var_name or not isinstance(value, ast.Call):
            continue
        if _name(value.func) != "ContextVar" or not value.args:
            continue
        first = value.args[0]
        if isinstance(first, ast.Constant) and first.value == env_name:
            return True
    return False


def _dict_maps_name(tree: ast.Module, dict_name: str, key: str, value_name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(_name(target) == dict_name for target in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for item_key, item_value in zip(node.value.keys, node.value.values):
            if (
                isinstance(item_key, ast.Constant)
                and item_key.value == key
                and _name(item_value) == value_name
            ):
                return True
    return False


def _function_has_set_call(
    tree: ast.Module,
    function_name: str,
    contextvar_name: str,
    arg_name: str,
) -> bool:
    func = _find_function(tree, function_name)
    if not func:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if _name(node.func.value) == contextvar_name and node.func.attr == "set":
            if node.args and _name(node.args[0]) == arg_name:
                return True
    return False


def _function_mentions_name(tree: ast.Module, function_name: str, name: str) -> bool:
    func = _find_function(tree, function_name)
    if not func:
        return False
    return any(isinstance(node, ast.Name) and node.id == name for node in ast.walk(func))


def _has_reply_sentinel_compare(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if _name(node.left) != "event.reply_to_text":
            continue
        if not any(isinstance(op, ast.Eq) for op in node.ops):
            continue
        if any(_name(comp) == "_REPLY_ORIGINAL_UNAVAILABLE" for comp in node.comparators):
            return True
    return False


def _has_string_constant(tree: ast.Module, needle: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if needle in node.value:
                return True
    return False


def _has_call_inside_if(tree: ast.Module, call_name: str, arg_name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for child in ast.walk(node.test):
            if isinstance(child, ast.Call) and _name(child.func) == call_name:
                if child.args and _name(child.args[0]) == arg_name:
                    return True
    return False


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_constant(node: ast.AST, value: object) -> bool:
    return (
        isinstance(node, ast.Constant)
        and type(node.value) is type(value)
        and node.value == value
    )


def _is_docstring(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_not_value(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _is_name(node.operand, "value")
    )


def _is_s_str_value_assignment(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and _is_name(node.targets[0], "s")
        and isinstance(node.value, ast.Call)
        and _name(node.value.func) == "str"
        and len(node.value.args) == 1
        and _is_name(node.value.args[0], "value")
        and not node.value.keywords
    )


def _is_in_compare(node: ast.AST, left_value: object, right_name: str) -> bool:
    return (
        isinstance(node, ast.Compare)
        and _is_constant(node.left, left_value)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.In)
        and len(node.comparators) == 1
        and _is_name(node.comparators[0], right_name)
    )


def _boolop_contains(node: ast.AST, predicate) -> bool:
    if isinstance(node, ast.BoolOp):
        return any(_boolop_contains(value, predicate) for value in node.values)
    return predicate(node)


def _is_dotdot_or_backslash_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    return (
        len(node.values) == 2
        and any(_is_in_compare(value, "..", "s") for value in node.values)
        and any(_is_in_compare(value, "\\", "s") for value in node.values)
    )


def _is_if_return(node: ast.AST, predicate, return_value: bool) -> bool:
    if not isinstance(node, ast.If) or not predicate(node.test):
        return False
    return (
        len(node.body) == 1
        and not node.orelse
        and isinstance(node.body[0], ast.Return)
        and _is_constant(node.body[0].value, return_value)
    )


def _is_startswith_root_or_home(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    if not (_is_name(node.func.value, "s") and node.func.attr == "startswith"):
        return False
    if len(node.args) != 1 or node.keywords or not isinstance(node.args[0], ast.Tuple):
        return False
    if len(node.args[0].elts) != 2:
        return False
    if not all(isinstance(item, ast.Constant) for item in node.args[0].elts):
        return False
    values = [item.value for item in node.args[0].elts]
    return values == ["/", "~"]


def _is_len_s_ge_two(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.GtE) or not node.comparators:
        return False
    left = node.left
    return (
        isinstance(left, ast.Call)
        and _name(left.func) == "len"
        and len(left.args) == 1
        and not left.keywords
        and _is_name(left.args[0], "s")
        and _is_constant(node.comparators[0], 2)
    )


def _is_s_index(node: ast.AST, index: int) -> bool:
    if not isinstance(node, ast.Subscript) or not _is_name(node.value, "s"):
        return False
    return _is_constant(node.slice, index)


def _is_s_slice(node: ast.AST, lower: int, upper: int) -> bool:
    if not isinstance(node, ast.Subscript) or not _is_name(node.value, "s"):
        return False
    if not isinstance(node.slice, ast.Slice):
        return False
    return (
        _is_constant(node.slice.lower, lower)
        and _is_constant(node.slice.upper, upper)
        and node.slice.step is None
    )


def _is_s0_isalpha(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "isalpha"
        and _is_s_index(node.func.value, 0)
        and not node.args
        and not node.keywords
    )


def _is_s1_colon(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and _is_s_index(node.left, 1)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and _is_constant(node.comparators[0], ":")
    )


def _is_drive_separator_check(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or not _is_s_slice(node.left, 2, 3):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.In):
        return False
    if len(node.comparators) != 1 or not isinstance(node.comparators[0], ast.Tuple):
        return False
    if len(node.comparators[0].elts) != 2:
        return False
    if not all(isinstance(item, ast.Constant) for item in node.comparators[0].elts):
        return False
    values = [item.value for item in node.comparators[0].elts]
    return values == ["/", "\\"]


def _is_session_key_return(node: ast.AST) -> bool:
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.BoolOp):
        return False
    if not isinstance(node.value.op, ast.And):
        return False
    checks = (
        _is_len_s_ge_two,
        _is_s0_isalpha,
        _is_s1_colon,
        _is_drive_separator_check,
    )
    return len(node.value.values) == 4 and all(
        check(value) for check, value in zip(checks, node.value.values)
    )


def _has_blanket_slash_reject(func: ast.FunctionDef) -> bool:
    return any(_is_in_compare(node, "/", "s") for node in ast.walk(func))


def _has_ordered_session_key_checker(func: ast.FunctionDef) -> bool:
    body = func.body
    index = 0
    if index < len(body) and _is_docstring(body[index]):
        index += 1
    if index < len(body) and _is_if_return(body[index], _is_not_value, False):
        index += 1
    return (
        index + 4 == len(body)
        and _is_s_str_value_assignment(body[index])
        and _is_if_return(body[index + 1], _is_dotdot_or_backslash_guard, True)
        and _is_if_return(body[index + 2], _is_startswith_root_or_home, True)
        and _is_session_key_return(body[index + 3])
    )


def _verify_session_key_checker_static(tree: ast.Module, text: str) -> bool:
    bindings = _top_level_name_bindings(tree, "_is_session_key_unsafe")
    if len(bindings) != 1 or not isinstance(bindings[0], ast.FunctionDef):
        return False
    if _has_dynamic_name_rebind_risk(tree, "_is_session_key_unsafe"):
        return False
    func = bindings[0]
    source = ast.get_source_segment(text, func)
    return (
        source == CANONICAL_SESSION_KEY_CHECKER
        and _has_ordered_session_key_checker(func)
        and not _has_blanket_slash_reject(func)
    )


def _validation_tuple_matches(
    node: ast.AST,
    field_name: str,
    value_name: str,
    checker_name: str,
) -> bool:
    if not isinstance(node, ast.Tuple) or len(node.elts) != 3:
        return False
    field, value, checker = node.elts
    return (
        isinstance(field, ast.Constant)
        and field.value == field_name
        and _name(value) == value_name
        and _name(checker) == checker_name
    )


def _is_checker_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _name(node.func) == "_checker"
        and len(node.args) == 1
        and _name(node.args[0]) == "_val"
    )


def _directly_raises_value_error(node: ast.If) -> bool:
    for child in node.body:
        if not isinstance(child, ast.Raise) or child.exc is None:
            continue
        exc = child.exc
        if _name(exc) == "ValueError":
            return True
        if isinstance(exc, ast.Call) and _name(exc.func) == "ValueError":
            return True
    return False


def _has_checker_guard_raise(node: ast.For) -> bool:
    for child in node.body:
        if isinstance(child, ast.If) and _is_checker_call(child.test):
            if _directly_raises_value_error(child):
                return True
    return False


def _loop_target_matches(node: ast.AST) -> bool:
    if not isinstance(node, ast.Tuple) or len(node.elts) != 3:
        return False
    return [_name(item) for item in node.elts] == ["_field", "_val", "_checker"]


def _has_session_path_validation_loop(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if not _loop_target_matches(node.target):
            continue
        if not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        has_session_key = any(
            _validation_tuple_matches(
                item, "session_key", "session_key", "_is_session_key_unsafe"
            )
            for item in node.iter.elts
        )
        has_session_id = any(
            _validation_tuple_matches(item, "session_id", "session_id", "_is_path_unsafe")
            for item in node.iter.elts
        )
        if has_session_key and has_session_id and _has_checker_guard_raise(node):
            return True
    return False


VERIFY_CHECKS = {
    "gateway/run.py": (
        (
            "run.reply_sentinel_constant",
            lambda tree, text: _has_constant_assign(
                tree,
                "_REPLY_ORIGINAL_UNAVAILABLE",
                "\x00__HERMES_REPLY_ORIGINAL_UNAVAILABLE__\x00",
            ),
        ),
        (
            "run.dingtalk_home_prompt_gate",
            lambda tree, text: (
                _find_function(tree, "_should_prompt_for_home_channel") is not None
                and "Platform.DINGTALK" in text
                and 'getattr(source, "chat_type", "") == "group"' in text
            ),
        ),
        (
            "run.reply_context_sentinel_branch",
            lambda tree, text: _has_reply_sentinel_compare(tree)
            and _has_string_constant(tree, "请先回顾上文"),
        ),
        (
            "run.home_prompt_condition",
            lambda tree, text: _has_call_inside_if(
                tree, "_should_prompt_for_home_channel", "source"
            ),
        ),
        (
            "run.session_env_fields",
            lambda tree, text: all(
                _has_call_keyword(tree, "set_session_vars", keyword)
                for keyword in ("chat_type", "user_id_alt", "session_id")
            ),
        ),
    ),
    "gateway/session.py": (
        (
            "session.session_key_validator",
            lambda tree, text: _verify_session_key_checker_static(tree, text),
        ),
        (
            "session.path_sensitive_validation",
            lambda tree, text: _has_session_path_validation_loop(tree),
        ),
    ),
    "gateway/session_context.py": (
        (
            "session_context.contextvars",
            lambda tree, text: _has_contextvar_assign(
                tree, "_SESSION_CHAT_TYPE", "HERMES_SESSION_CHAT_TYPE"
            )
            and _has_contextvar_assign(
                tree, "_SESSION_USER_ID_ALT", "HERMES_SESSION_USER_ID_ALT"
            ),
        ),
        (
            "session_context.var_map",
            lambda tree, text: _dict_maps_name(
                tree, "_VAR_MAP", "HERMES_SESSION_CHAT_TYPE", "_SESSION_CHAT_TYPE"
            )
            and _dict_maps_name(
                tree, "_VAR_MAP", "HERMES_SESSION_USER_ID_ALT", "_SESSION_USER_ID_ALT"
            ),
        ),
        (
            "session_context.set_session_vars_signature",
            lambda tree, text: _has_function_arg(tree, "set_session_vars", "chat_type")
            and _has_function_arg(tree, "set_session_vars", "user_id_alt"),
        ),
        (
            "session_context.set_session_vars_tokens",
            lambda tree, text: _function_has_set_call(
                tree, "set_session_vars", "_SESSION_CHAT_TYPE", "chat_type"
            )
            and _function_has_set_call(
                tree, "set_session_vars", "_SESSION_USER_ID_ALT", "user_id_alt"
            ),
        ),
        (
            "session_context.clear_session_vars",
            lambda tree, text: _function_mentions_name(
                tree, "clear_session_vars", "_SESSION_CHAT_TYPE"
            )
            and _function_mentions_name(
                tree, "clear_session_vars", "_SESSION_USER_ID_ALT"
            ),
        ),
    ),
}


def resolve_target(target: Path) -> Path:
    """Return a Hermes root containing ``gateway/*.py``."""
    root = target.resolve()
    if (root / "gateway").is_dir():
        return root
    if (root / "hermes" / "gateway").is_dir():
        return root / "hermes"
    raise FileNotFoundError(
        f"{target} does not look like a Hermes root: expected gateway/ or hermes/gateway/"
    )


def _file_report(path: Path) -> dict[str, str | int | bool]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "sha256": _sha256(path) if path.exists() else "",
        "bytes": path.stat().st_size if path.exists() else 0,
    }


def _atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def verify(root: Path) -> tuple[bool, list[StepResult]]:
    results: list[StepResult] = []
    ok = True
    for rel_path, checks in VERIFY_CHECKS.items():
        path = root / rel_path
        if not path.exists():
            ok = False
            results.append(
                StepResult(
                    name=f"verify.{rel_path}",
                    path=rel_path,
                    status="missing-file",
                    message=f"required file not found: {rel_path}",
                )
            )
            continue
        tree, parse_error = _parse_python(path)
        if tree is None:
            ok = False
            results.append(
                StepResult(
                    name=f"verify.{rel_path}",
                    path=rel_path,
                    status="syntax-error",
                    message=parse_error,
                )
            )
            continue
        text = path.read_text(encoding="utf-8")
        for check_name, check in checks:
            try:
                passed = check(tree, text)
            except Exception as exc:  # noqa: BLE001 - verifier boundary
                passed = False
                message = f"{type(exc).__name__}: {exc}"
            else:
                message = ""
            if passed:
                results.append(
                    StepResult(name=check_name, path=rel_path, status="ok")
                )
            else:
                ok = False
                results.append(
                    StepResult(
                        name=check_name,
                        path=rel_path,
                        status="missing-structure",
                        message=message or "required compat structure not found",
                    )
                )
    return ok, results


def apply_steps(root: Path, *, dry_run: bool = False) -> tuple[bool, list[StepResult], set[str]]:
    results: list[StepResult] = []
    changed_paths: set[str] = set()
    ok = True
    texts: dict[str, str] = {}

    for step in STEPS:
        path = root / step.path
        if not path.exists():
            ok = False
            results.append(
                StepResult(
                    name=step.name,
                    path=step.path,
                    status="missing-file",
                    message=f"required file not found: {step.path}",
                )
            )
            continue

        text = texts.setdefault(step.path, path.read_text(encoding="utf-8"))
        if _all_present(text, step.markers()):
            results.append(StepResult(name=step.name, path=step.path, status="present"))
            continue
        replacement = next(
            ((old, new) for old, new in step.replacements() if old in text),
            None,
        )
        if replacement is None:
            ok = False
            results.append(
                StepResult(
                    name=step.name,
                    path=step.path,
                    status="anchor-missing",
                    message="marker absent and source anchor did not match",
                )
            )
            continue
        old, new = replacement
        texts[step.path] = text.replace(old, new, 1)
        changed_paths.add(step.path)
        results.append(
            StepResult(
                name=step.name,
                path=step.path,
                status="would-change" if dry_run else "changed",
            )
        )

    if ok and not dry_run:
        originals = {
            rel_path: (root / rel_path).read_text(encoding="utf-8")
            for rel_path in changed_paths
        }
        written: list[str] = []
        for rel_path in sorted(changed_paths):
            try:
                _atomic_write(root / rel_path, texts[rel_path])
                written.append(rel_path)
            except Exception as exc:
                for written_path in reversed(written):
                    _atomic_write(root / written_path, originals[written_path])
                ok = False
                results.append(
                    StepResult(
                        name="apply.write_files",
                        path=rel_path,
                        status="write-failed",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
                changed_paths.clear()
                break

    return ok, results, changed_paths


def build_report(target: Path, mode: Mode, *, dry_run: bool = False) -> dict[str, object]:
    root = resolve_target(target)
    before = {_path: _file_report(root / _path) for _path in VERIFY_PATHS}
    effective_dry_run = dry_run or mode == "check"

    if mode == "verify":
        ok, results = verify(root)
        changed_paths: set[str] = set()
    else:
        ok, results, changed_paths = apply_steps(root, dry_run=effective_dry_run)
        if mode == "apply" and ok and not dry_run:
            verified, verify_results = verify(root)
            ok = ok and verified
            results.extend(verify_results)

    after = {_path: _file_report(root / _path) for _path in VERIFY_PATHS}
    failures = [
        result
        for result in results
        if result.status
        in {
            "missing-file",
            "anchor-missing",
            "missing-markers",
            "missing-structure",
            "syntax-error",
            "write-failed",
        }
    ]

    return {
        "ok": ok and not failures,
        "mode": mode,
        "dry_run": effective_dry_run,
        "target": str(root),
        "changed_files": sorted(changed_paths),
        "changed_count": len(changed_paths),
        "failure_count": len(failures),
        "results": [result.to_dict() for result in results],
        "backup_manifest": {
            "type": "sha256-before-after",
            "before": before,
            "after": after,
        },
    }


def _text_summary(report: dict[str, object]) -> str:
    status = "ok" if report["ok"] else "failed"
    lines = [
        f"compat patcher {status}",
        f"mode={report['mode']} dry_run={report['dry_run']} changed_count={report['changed_count']} failure_count={report['failure_count']}",
        f"target={report['target']}",
    ]
    changed_files = report.get("changed_files") or []
    if changed_files:
        lines.append("changed_files=" + ",".join(str(path) for path in changed_files))
    manifest = report.get("backup_manifest") or {}
    if isinstance(manifest, dict):
        manifest_type = manifest.get("type")
        before = manifest.get("before") or {}
        if manifest_type and isinstance(before, dict):
            lines.append(
                f"backup_manifest={manifest_type} files={len(before)} "
                "(use --json for sha256 details)"
            )
    for result in report["results"]:  # type: ignore[index]
        item = result  # type: ignore[assignment]
        suffix = f" ({item['message']})" if item.get("message") else ""
        lines.append(f"- {item['status']}: {item['name']} [{item['path']}]{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Hermes DingTalk compat patches.")
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Hermes root containing gateway/, or a parent containing hermes/gateway/.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check anchors without writing.")
    mode.add_argument("--apply", action="store_true", help="Apply missing compat patches.")
    mode.add_argument("--verify", action="store_true", help="Require all compat structures to exist.")
    parser.add_argument("--dry-run", action="store_true", help="Plan apply without writing.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    selected: Mode = "verify"
    if args.check:
        selected = "check"
    elif args.apply:
        selected = "apply"

    try:
        report = build_report(args.target, selected, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        report = {
            "ok": False,
            "mode": selected,
            "dry_run": args.dry_run,
            "target": str(args.target),
            "changed_files": [],
            "changed_count": 0,
            "failure_count": 1,
            "results": [
                {
                    "name": "compat_patcher.startup",
                    "path": str(args.target),
                    "status": "error",
                    "message": str(exc),
                }
            ],
            "backup_manifest": {"type": "sha256-before-after", "before": {}, "after": {}},
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_text_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
