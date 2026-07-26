"""Behavior contract for DingTalk reply-context fail-closed handling."""

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
REPLY_CONTEXT_PATH = PLUGIN_ROOT / "reply_context.py"


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
    DOCUMENT = "document"


def load_reply_context():
    name = "dingtalk_reply_context_resolution_uut"
    spec = importlib.util.spec_from_file_location(name, REPLY_CONTEXT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_stamp_group_text():
    """The real group-text stamp helper the adapter imports from mentions.py."""
    name = "dingtalk_mentions_for_resolution_uut"
    spec = importlib.util.spec_from_file_location(name, MENTIONS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.stamp_group_text


def load_on_message(reply_context):
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
    method = None
    clarification = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_REPLY_CONTEXT_CLARIFICATION"
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
    if not isinstance(clarification, str) or not clarification:
        raise RuntimeError("_REPLY_CONTEXT_CLARIFICATION not found")

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

    def extract_media(message, message_type):
        extensions = getattr(getattr(message, "text", None), "extensions", {}) or {}
        replied = extensions.get("repliedMsg") or {}
        if str(replied.get("msgType") or "").lower() == "file":
            return message_type.DOCUMENT, ["resolved-file"], ["text/plain"]
        return message_type.TEXT, [], []

    namespace = {
        "MessageEvent": FakeMessageEvent,
        "MessageType": MessageType,
        "_DINGTALK_WEBHOOK_RE": re.compile(r"^https://api\.dingtalk\.com/"),
        "_SESSION_WEBHOOKS_MAX": 500,
        "_REPLY_CONTEXT_CLARIFICATION": clarification,
        "_REPLY_ORIGINAL_UNAVAILABLE": reply_context._REPLY_ORIGINAL_UNAVAILABLE,
        "_forwarded_chat_text_from_raw": lambda *args, **kwargs: "",
        "_is_placeholder_text": lambda value: False,
        "_log_forward_diag": lambda *args, **kwargs: None,
        "build_reply_kwargs": reply_context.build_reply_kwargs,
        "datetime": datetime,
        "extract_media": extract_media,
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: "",
        "should_process_message": lambda *args, **kwargs: True,
        "stamp_group_text": load_stamp_group_text(),
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self, *, send_success=True):
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


def make_message(*, replied=None):
    return SimpleNamespace(
        message_id="incoming-1",
        conversation_id="conversation-1",
        conversation_type="1",
        sender_id="sender-1",
        sender_nick="Sender",
        sender_staff_id="staff-1",
        session_webhook="https://api.dingtalk.com/session-webhook",
        session_webhook_expired_time=0,
        create_at=None,
        message_type="text",
        text=SimpleNamespace(
            content="这个怎么处理",
            extensions={"repliedMsg": replied} if replied else {},
        ),
        _hermes_raw_data={},
    )


class DingTalkReplyResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reply_context = load_reply_context()
        cls.on_message = staticmethod(load_on_message(cls.reply_context))

    def run_message(self, message, *, send_success=True):
        adapter = FakeAdapter(send_success=send_success)
        asyncio.run(self.on_message(adapter, message))
        return adapter

    def test_missing_original_clarifies_and_never_starts_model(self):
        adapter = self.run_message(
            make_message(replied={"msgId": "quoted-1", "msgType": "text"})
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("conversation-1", adapter.sent[0]["chat_id"])
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])
        self.assertIn("没有拿到你引用的原文", adapter.sent[0]["content"])

    def test_clarification_delivery_failure_still_never_starts_model(self):
        adapter = self.run_message(
            make_message(replied={"msgId": "quoted-1", "msgType": "text"}),
            send_success=False,
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))

    def test_callback_original_reaches_model_without_clarification(self):
        adapter = self.run_message(
            make_message(
                replied={
                    "msgId": "quoted-2",
                    "msgType": "text",
                    "content": {"text": "供应商联系人没有显示"},
                }
            )
        )

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("供应商联系人没有显示", adapter.events[0].reply_to_text)

    def test_non_reply_message_is_unchanged(self):
        adapter = self.run_message(make_message())

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertFalse(hasattr(adapter.events[0], "reply_to_text"))

    def test_file_reply_stays_on_media_path(self):
        adapter = self.run_message(
            make_message(
                replied={
                    "msgId": "quoted-file",
                    "msgType": "file",
                    "content": {"fileName": "evidence.txt", "downloadCode": "code-1"},
                }
            )
        )

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual(MessageType.DOCUMENT, adapter.events[0].message_type)
        self.assertFalse(hasattr(adapter.events[0], "reply_to_text"))


if __name__ == "__main__":
    unittest.main()
