"""Behavior contract for the DingTalk-to-Core natural task intake seam."""

from __future__ import annotations

import ast
import asyncio
import copy
import re
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"


class FakeLogger:
    def debug(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


class FakeMessageEvent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MessageType:
    TEXT = "text"


def load_on_message():
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
        "datetime": datetime,
        "extract_media": lambda message, message_type: (message_type.TEXT, [], []),
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: "",
        "resolve_task_binding": resolve_task_binding,
        "should_process_message": lambda *args, **kwargs: True,
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self, *, enabled, send_success=True):
        self.name = "dingtalk"
        self.config = SimpleNamespace(extra={"natural_task_intake": enabled})
        self._allowed_users = set()
        self._mention_patterns = []
        self._dedup = SimpleNamespace(is_duplicate=lambda message_id: False)
        self._message_contexts = {}
        self._done_emoji_fired = set()
        self._session_webhooks = {}
        self._session_store = object()
        self.events = []
        self.sent = []
        self.send_success = send_success

    @staticmethod
    def _extract_text(message):
        return getattr(getattr(message, "text", None), "content", "")

    async def _resolve_media_codes(self, message):
        return None

    @staticmethod
    def build_source(**kwargs):
        return kwargs

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
        return SimpleNamespace(success=self.send_success)


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


class DingTalkNaturalTaskIntakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.on_message = staticmethod(load_on_message())

    def run_message(
        self,
        text,
        *,
        enabled=True,
        result=None,
        raises=False,
        send_success=True,
    ):
        calls = []

        def resolver(session_store, source, message_text, request_id, **kwargs):
            calls.append(
                {
                    "session_store": session_store,
                    "source": source,
                    "text": message_text,
                    "request_id": request_id,
                    "kwargs": kwargs,
                }
            )
            if raises:
                raise RuntimeError("resolver failed")
            return result or SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        task_intake = types.ModuleType("gateway.task_intake")
        task_intake.resolve_natural_task_intake = resolver
        old_gateway = sys.modules.get("gateway")
        old_task_intake = sys.modules.get("gateway.task_intake")
        sys.modules["gateway"] = gateway
        sys.modules["gateway.task_intake"] = task_intake
        try:
            adapter = FakeAdapter(enabled=enabled, send_success=send_success)
            asyncio.run(self.on_message(adapter, make_message(text)))
        finally:
            if old_gateway is None:
                sys.modules.pop("gateway", None)
            else:
                sys.modules["gateway"] = old_gateway
            if old_task_intake is None:
                sys.modules.pop("gateway.task_intake", None)
            else:
                sys.modules["gateway.task_intake"] = old_task_intake
        return adapter, calls

    def test_feature_off_is_byte_compatible_and_never_calls_resolver(self):
        adapter, calls = self.run_message("普通聊天", enabled=False)

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("普通聊天", adapter.events[0].text)
        self.assertNotIn("board_slug", adapter.events[0].source)

    def test_reply_without_agent_sends_once_and_stops(self):
        result = SimpleNamespace(
            action="reply_without_agent",
            source=None,
            text="",
            reply_text="我找到 2 个任务，请选择 1 或 2。",
        )
        adapter, calls = self.run_message("联系人为什么没显示", result=result)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])
        self.assertEqual(result.reply_text, adapter.sent[0]["content"])

    def test_error_without_agent_stops_even_when_delivery_fails(self):
        result = SimpleNamespace(
            action="error_without_agent",
            source=None,
            text="",
            reply_text="任务账本暂时无法安全检索。",
        )
        adapter, calls = self.run_message(
            "联系人为什么没显示",
            result=result,
            send_success=False,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))

    def test_bound_source_reaches_the_same_gateway_event(self):
        bound = {
            "chat_id": "conversation-1",
            "chat_type": "group",
            "user_id": "sender-1",
            "message_id": "incoming-1",
            "board_slug": "agong",
            "task_id": "t_deadbeef",
        }
        result = SimpleNamespace(
            action="bound_source",
            source=bound,
            text="联系人为什么没显示",
            reply_text=None,
        )
        adapter, calls = self.run_message("对", result=result)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIs(bound, adapter.events[0].source)
        self.assertEqual("联系人为什么没显示", adapter.events[0].text)

    def test_pass_through_keeps_the_original_source_and_text(self):
        result = SimpleNamespace(
            action="pass_through",
            source={"board_slug": "must-not-leak"},
            text="must-not-replace",
            reply_text=None,
        )
        adapter, calls = self.run_message("普通聊天", result=result)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("普通聊天", adapter.events[0].text)
        self.assertNotIn("board_slug", adapter.events[0].source)

    def test_raw_binding_bypasses_natural_resolver_and_stays_per_message(self):
        adapter, calls = self.run_message(
            "#任务 agong/t_deadbeef 联系人为什么没显示"
        )

        self.assertEqual([], calls)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("agong", adapter.events[0].source["board_slug"])
        self.assertEqual("t_deadbeef", adapter.events[0].source["task_id"])
        self.assertEqual("联系人为什么没显示", adapter.events[0].text)

    def test_slash_command_bypasses_natural_resolver(self):
        adapter, calls = self.run_message("/new")

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("/new", adapter.events[0].text)

    def test_resolver_exception_fails_closed_before_agent(self):
        adapter, calls = self.run_message("联系人为什么没显示", raises=True)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("任务接入暂时不可用", adapter.sent[0]["content"])


if __name__ == "__main__":
    unittest.main()
