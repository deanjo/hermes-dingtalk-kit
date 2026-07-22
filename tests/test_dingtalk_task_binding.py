"""Behavior contracts for explicit DingTalk-to-Kanban task binding."""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import re
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "overlays/hermes/plugins/platforms/dingtalk"
ADAPTER_PATH = PLUGIN_ROOT / "adapter.py"
BINDING_PATH = PLUGIN_ROOT / "task_binding.py"


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
    name = "dingtalk_task_binding_uut"
    spec = importlib.util.spec_from_file_location(name, BINDING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_on_message(binding, *, exists):
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
    method = None
    clarification = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_TASK_BINDING_CLARIFICATION"
            for target in node.targets
        ):
            clarification = ast.literal_eval(node.value)
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
    if not isinstance(clarification, str) or "#任务" not in clarification:
        raise RuntimeError("_TASK_BINDING_CLARIFICATION not found")

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

    namespace = {
        "MessageEvent": FakeMessageEvent,
        "MessageType": MessageType,
        "_DINGTALK_WEBHOOK_RE": re.compile(r"^https://api\.dingtalk\.com/"),
        "_REPLY_CONTEXT_CLARIFICATION": "reply clarification",
        "_REPLY_ORIGINAL_UNAVAILABLE": "reply unavailable",
        "_SESSION_WEBHOOKS_MAX": 500,
        "_TASK_BINDING_CLARIFICATION": clarification,
        "_forwarded_chat_text_from_raw": lambda *args, **kwargs: "",
        "_is_placeholder_text": lambda value: False,
        "_log_forward_diag": lambda *args, **kwargs: None,
        "asyncio": asyncio,
        "build_reply_kwargs": lambda message: {},
        "datetime": datetime,
        "extract_media": lambda message, message_type: (message_type.TEXT, [], []),
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: "",
        "resolve_task_binding": binding.resolve_task_binding,
        "run_natural_intake_gate": binding.run_natural_intake_gate,
        "should_process_message": lambda *args, **kwargs: True,
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    binding.task_binding_exists = lambda board_slug, task_id: exists
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self):
        self.name = "dingtalk"
        self.config = SimpleNamespace(extra={})
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
        """Exact signature mirror of Core ``BasePlatformAdapter.build_source``.

        No ``**kwargs`` on purpose: any kwarg the adapter passes that Core
        does not accept must raise ``TypeError`` here, so signature drift
        between this overlay and Core turns the suite red.
        """
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


def make_message(text):
    return SimpleNamespace(
        message_id="incoming-1",
        conversation_id="conversation-1",
        conversation_type="2",
        sender_id="sender-1",
        sender_nick="Sender",
        sender_staff_id="staff-1",
        session_webhook="https://api.dingtalk.com/session-webhook",
        session_webhook_expired_time=0,
        create_at=None,
        message_type="text",
        text=SimpleNamespace(content=text, extensions={}),
        _hermes_raw_data={},
    )


class TaskBindingParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binding = load_binding_module()

    def test_unbound_text_is_unchanged(self):
        parsed = self.binding.parse_task_binding("普通聊天")
        self.assertIsNone(parsed.binding)
        self.assertEqual("普通聊天", parsed.message_text)

    def test_valid_prefix_is_parsed_and_removed_from_model_text(self):
        parsed = self.binding.parse_task_binding(
            "#任务 agong/t_deadbeef 联系人为什么没显示"
        )
        self.assertEqual("agong", parsed.binding.board_slug)
        self.assertEqual("t_deadbeef", parsed.binding.task_id)
        self.assertEqual("联系人为什么没显示", parsed.message_text)

    def test_malformed_binding_is_rejected(self):
        for text in (
            "#任务 ../agong/t_deadbeef 查询",
            "#任务 agong/not-a-task 查询",
            "#任务 agong/t_deadbeef",
        ):
            with self.subTest(text=text):
                with self.assertRaises(self.binding.TaskBindingError):
                    self.binding.parse_task_binding(text)


class TaskBindingAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binding = load_binding_module()

    def run_message(self, text, *, exists):
        adapter = FakeAdapter()
        on_message = load_on_message(self.binding, exists=exists)
        asyncio.run(on_message(adapter, make_message(text)))
        return adapter

    def test_valid_existing_binding_reaches_gateway_with_explicit_source(self):
        adapter = self.run_message(
            "#任务 agong/t_deadbeef 联系人为什么没显示",
            exists=True,
        )
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("联系人为什么没显示", adapter.events[0].text)
        self.assertEqual("agong", adapter.events[0].source.board_slug)
        self.assertEqual("t_deadbeef", adapter.events[0].source.task_id)

    def test_unknown_binding_clarifies_once_and_never_starts_model(self):
        adapter = self.run_message(
            "#任务 agong/t_deadbeef 联系人为什么没显示",
            exists=False,
        )
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("#任务", adapter.sent[0]["content"])

    def test_malformed_binding_clarifies_once_and_never_starts_model(self):
        adapter = self.run_message("#任务 agong/not-a-task 查询", exists=True)
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))

    def test_normal_chat_stays_on_legacy_path(self):
        adapter = self.run_message("普通聊天", exists=False)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.board_slug)
        self.assertEqual("普通聊天", adapter.events[0].text)

    def test_binding_lookup_runs_off_the_event_loop(self):
        """A locked Kanban SQLite lookup must not stall the event loop.

        ``task_binding_exists`` opens the board database with sqlite3's
        default 5s lock timeout; held synchronously it froze the gateway
        loop for the full timeout. It must be offloaded like the natural
        resolver: a 20ms tick scheduled next to a 250ms lookup has to fire
        BEFORE the lookup returns.
        """
        lookup_ended_at = []
        tick_fired_at = []

        def slow_exists(board_slug, task_id):
            time.sleep(0.25)
            lookup_ended_at.append(time.monotonic())
            return True

        on_message = load_on_message(self.binding, exists=True)
        self.binding.task_binding_exists = slow_exists

        async def main():
            adapter = FakeAdapter()

            async def ticker():
                await asyncio.sleep(0.02)
                tick_fired_at.append(time.monotonic())

            await asyncio.gather(
                on_message(adapter, make_message("#任务 agong/t_deadbeef 查询")),
                ticker(),
            )
            return adapter

        try:
            adapter = asyncio.run(main())
        finally:
            self.binding.task_binding_exists = lambda board_slug, task_id: True

        self.assertEqual(1, len(lookup_ended_at))
        self.assertEqual(1, len(tick_fired_at))
        self.assertLess(
            tick_fired_at[0],
            lookup_ended_at[0],
            "event loop tick was blocked by the synchronous binding lookup",
        )
        # Semantics unchanged: a valid existing binding still reaches the gateway.
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("agong", adapter.events[0].source.board_slug)


if __name__ == "__main__":
    unittest.main()
