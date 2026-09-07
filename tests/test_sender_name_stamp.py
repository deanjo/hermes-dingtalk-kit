"""Behavior contract for the group-message sender-name stamp (F1).

Incident: a user asked Hermes to find what 冯艳 said; session_search found
nothing because group messages were persisted without the sender's display
name (FTS5 trigram needs ≥3 chars; the 2-char CJK LIKE fallback can only
match text that is actually stored).  The adapter now stamps group message
text with ``<senderNick>: `` before it reaches the gateway, so the persisted
transcript — and therefore session_search — carries the sender identity.

Pinned here:
  - group text is stamped; DM text is not; slash commands keep their "/"
  - existing injections (mention meta, 消息编号/状态注记) survive the stamp
  - Core's shared-session ``[user_name]`` prefix must not double up — that
    dedup lives in Core (tests/gateway/test_shared_group_sender_prefix.py);
    here we only pin the exact stamp shape Core's guard keys off
    (``startswith(f"{user_name}:")``).
"""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "overlays/hermes/plugins/platforms/dingtalk"
ADAPTER_PATH = PLUGIN_ROOT / "adapter.py"
BINDING_PATH = PLUGIN_ROOT / "task_binding.py"
MENTIONS_PATH = PLUGIN_ROOT / "mentions.py"


class FakeLogger:
    def debug(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


class FakeMessageEvent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MessageType:
    TEXT = "text"


class FakeSource:
    """Attribute mirror of the Core SessionSource fields this path touches."""

    profile = None
    board_slug = None
    task_id = None

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def load_binding_module():
    name = "dingtalk_task_binding_for_stamp_uut"
    spec = importlib.util.spec_from_file_location(name, BINDING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_stamp_group_text():
    """The real helper under test — the adapter imports it from mentions.py."""
    name = "dingtalk_mentions_for_stamp_uut"
    spec = importlib.util.spec_from_file_location(name, MENTIONS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.stamp_group_text


def load_on_message(*, natural_intake=False, with_media=False, mention_meta="", binding=None):
    """AST-extract DingTalkAdapter._on_message with a fake namespace, the same
    harness style as test_dingtalk_task_binding.py / test_dingtalk_natural_task_intake.py."""
    spec = importlib.util.spec_from_file_location("reply_context_for_sender_stamp", PLUGIN_ROOT / "reply_context.py")
    reply_context = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reply_context)
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
    method = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter":
            method = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "_on_message"
                ),
                None,
            )
            break
    if method is None:
        raise RuntimeError("DingTalkAdapter._on_message not found")

    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            copy.deepcopy(method),
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)

    async def resolve_task_binding(adapter, text, **kwargs):
        del adapter, kwargs
        reference, message_text = text.split(maxsplit=2)[1:]
        board_slug, task_id = reference.split("/", 1)
        return SimpleNamespace(
            binding=SimpleNamespace(board_slug=board_slug, task_id=task_id),
            message_text=message_text,
        )

    if with_media:
        def extract_media(message, message_type):
            return message_type.TEXT, ["https://cdn.example.com/img.png"], ["image"]
    else:
        def extract_media(message, message_type):
            return message_type.TEXT, [], []

    async def restore_h1_binding(adapter, source):
        return source

    namespace = {
        "MessageEvent": FakeMessageEvent,
        "MessageType": MessageType,
        "_DINGTALK_WEBHOOK_RE": re.compile(r"^https://api\.dingtalk\.com/"),
        "_REPLY_CONTEXT_CLARIFICATION": "reply clarification",
        "_REPLY_ORIGINAL_UNAVAILABLE": "reply unavailable",
        "_SESSION_WEBHOOKS_MAX": 500,
        "_TASK_BINDING_CLARIFICATION": "binding clarification",
        "_forwarded_chat_text_from_raw": lambda *args, **kwargs: "",
        "_is_placeholder_text": lambda value: False,
        "_log_forward_diag": lambda *args, **kwargs: None,
        "asyncio": asyncio,
        "build_reply_kwargs": lambda message: {},
        "append_full_reply_text": reply_context.append_full_reply_text,
        "datetime": datetime,
        "extract_media": extract_media,
        "h1_turn_meta_lines": lambda adapter, **kwargs: ["[消息编号: incoming-1]"],
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: mention_meta,
        "resolve_task_binding": (
            binding.resolve_task_binding if binding is not None else resolve_task_binding
        ),
        "restore_h1_binding": restore_h1_binding,
        "set_h1_dispatch_scope": lambda **kwargs: None,
        "should_process_message": lambda *args, **kwargs: True,
        "stamp_group_text": load_stamp_group_text(),
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self, *, natural_intake=False):
        self.name = "dingtalk"
        self.config = SimpleNamespace(
            extra={"natural_task_intake": True} if natural_intake else {}
        )
        self._allowed_users = set()
        self._mention_patterns = []
        self._dedup = SimpleNamespace(is_duplicate=lambda message_id: False)
        self._message_contexts = {}
        self._done_emoji_fired = set()
        self._session_webhooks = {}
        self.events = []
        self.sent = []

    @staticmethod
    def _extract_text(message):
        return getattr(getattr(message, "text", None), "content", "")

    async def _resolve_media_codes(self, message):
        return None

    @staticmethod
    def build_source(
        chat_id,
        chat_name=None,
        chat_type="dm",
        user_id=None,
        user_name=None,
        thread_id=None,
        chat_topic=None,
        user_id_alt=None,
        chat_id_alt=None,
        is_bot=False,
        guild_id=None,
        parent_chat_id=None,
        message_id=None,
        role_authorized=False,
        auto_thread_created=False,
        auto_thread_initial_name=None,
    ):
        """Exact signature mirror of Core ``BasePlatformAdapter.build_source``."""
        return FakeSource(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
            user_id_alt=user_id_alt,
            chat_id_alt=chat_id_alt,
            is_bot=is_bot,
            guild_id=guild_id,
            parent_chat_id=parent_chat_id,
            message_id=message_id,
            role_authorized=role_authorized,
            auto_thread_created=auto_thread_created,
            auto_thread_initial_name=auto_thread_initial_name,
        )

    async def handle_message(self, event):
        self.events.append(event)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(success=True)


def make_message(text, *, conversation_type="2", sender_nick="冯艳", **extra_attrs):
    return SimpleNamespace(
        message_id="incoming-1",
        conversation_id="conversation-1",
        conversation_type=conversation_type,
        sender_id="sender-1",
        sender_nick=sender_nick,
        sender_staff_id="staff-1",
        session_webhook="https://api.dingtalk.com/session-webhook",
        session_webhook_expired_time=0,
        create_at=None,
        message_type="text",
        text=SimpleNamespace(content=text, extensions={}),
        _hermes_raw_data={},
        **extra_attrs,
    )


class SenderNameStampTest(unittest.TestCase):
    def run_message(self, text, *, on_message=None, natural_intake=False, **msg_kwargs):
        adapter = FakeAdapter(natural_intake=natural_intake)
        handler = on_message or load_on_message(natural_intake=natural_intake)
        asyncio.run(handler(adapter, make_message(text, **msg_kwargs)))
        return adapter

    def test_group_text_stamped_with_sender_nick(self):
        """The incident scenario: 冯艳's group message must persist her name."""
        adapter = self.run_message("天津ABB的报价单发了吗")
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("冯艳: 天津ABB的报价单发了吗", adapter.events[0].text)

    def test_stamp_shape_matches_core_dedup_guard(self):
        """Core's shared-session guard skips its own "[user_name]" prefix when
        the text already starts with "{user_name}:" — pin that exact shape
        (source.user_name is sender_nick via build_source)."""
        adapter = self.run_message("在吗")
        event = adapter.events[0]
        self.assertTrue(event.text.startswith(f"{event.source.user_name}:"))

    def test_dm_text_not_stamped(self):
        adapter = self.run_message("报价单发了吗", conversation_type="1")
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("报价单发了吗", adapter.events[0].text)

    def test_slash_command_not_stamped(self):
        """A stamp would hide the leading "/" from gateway command parsing."""
        adapter = self.run_message("/new")
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("/new", adapter.events[0].text)

    def test_mention_meta_appended_after_stamp(self):
        on_message = load_on_message(mention_meta="[@了: 张三]")
        adapter = self.run_message("看下这个", on_message=on_message)
        self.assertEqual("冯艳: 看下这个\n\n[@了: 张三]", adapter.events[0].text)

    def test_inbound_number_meta_preserved_with_stamp(self):
        """V2 structural meta (消息编号/状态注记) still lands after the stamp."""
        adapter = self.run_message("在吗", natural_intake=True)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("冯艳: 在吗\n\n[消息编号: incoming-1]", adapter.events[0].text)

    def test_media_only_message_stamped_with_bare_nick(self):
        """No trailing ": " when there is no text body to attach it to."""
        on_message = load_on_message(with_media=True)
        adapter = self.run_message("", on_message=on_message)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("冯艳", adapter.events[0].text)

    def test_task_binding_remainder_stamped(self):
        """#任务 parsing runs before the stamp; the bound remainder is stamped."""
        binding = load_binding_module()
        on_message = load_on_message(binding=binding)
        binding.task_binding_exists = lambda board_slug, task_id: True
        adapter = self.run_message("#任务 agong/t_deadbeef 联系人为什么没显示", on_message=on_message)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("冯艳: 联系人为什么没显示", adapter.events[0].text)
        self.assertEqual("agong", adapter.events[0].source.board_slug)

    def test_missing_nick_falls_back_to_sender_id(self):
        """sender_nick falls back to sender_id (same as source.user_name)."""
        adapter = self.run_message("在吗", sender_nick="")
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("sender-1: 在吗", adapter.events[0].text)


class ConversationTitleChatNameTest(unittest.TestCase):
    """Group callback ``conversationTitle`` -> ``source.chat_name`` contract.

    session_search browse locates group sessions by the sessions table
    ``display_name``, which Core's SessionStore stamps from
    ``source.chat_name``.  The group name only reaches Core because the
    adapter maps the DingTalk callback's ``conversationTitle`` (exposed by
    the dingtalk-stream SDK as ``conversation_title``) into
    ``build_source(chat_name=...)``.  Pin that mapping so a refactor cannot
    silently regress display_name back to NULL (user-report-20260726).
    """

    def test_group_conversation_title_becomes_chat_name(self):
        adapter = FakeAdapter()
        asyncio.run(
            load_on_message()(
                adapter,
                make_message("报价单发了吗", conversation_title="A供问题日常沟通群"),
            )
        )
        self.assertEqual(1, len(adapter.events))
        source = adapter.events[0].source
        self.assertEqual("A供问题日常沟通群", source.chat_name)
        self.assertEqual("group", source.chat_type)

    def test_missing_conversation_title_yields_none_chat_name(self):
        """DMs and title-less callbacks must pass None, not a fabricated name."""
        adapter = FakeAdapter()
        asyncio.run(load_on_message()(adapter, make_message("在吗")))
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.chat_name)


if __name__ == "__main__":
    unittest.main()
