"""Behavior contract for the DingTalk V2 thin gate (bare agent + thin gates).

V2 (H1_V2_BARE_AGENT_DESIGN): the intent-classifier bridge and intake gate
were removed.  This file pins what the thin gate does now: feature-flag
pass-through, anonymous-sender skip, per-message read-only binding restore
(M3 degrade-unbound), structural meta injection (inbound number + delivery
receipt), reply-context passthrough, the T1 sentinel clarification, and
the real import chain (no sys.modules doubles).
"""

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
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"
BINDING_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/task_binding.py"
REPLY_CONTEXT_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/reply_context.py"
SESSION_PATH = ROOT / "overlays/hermes/gateway/session.py"


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


class FakeSource:
    """Attribute mirror of the Core SessionSource fields this path touches."""

    profile = None
    board_slug = None
    task_id = None

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def load_reply_context_module():
    import importlib.util

    name = "dingtalk_reply_context_for_gate_uut"
    spec = importlib.util.spec_from_file_location(name, REPLY_CONTEXT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    sys.modules.setdefault("reply_context", module)
    return module


def load_task_binding_module():
    """Load the real task_binding.py helpers under test (thin gate drives
    the real implementations — only ``gateway.task_intake`` is faked)."""
    import importlib.util

    load_reply_context_module()
    name = "dingtalk_task_binding_for_gate_uut"
    spec = importlib.util.spec_from_file_location(name, BINDING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_on_message(*, with_media=False):
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

    task_binding_module = load_task_binding_module()
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
        "build_reply_kwargs": lambda message: getattr(message, "_test_reply_kwargs", None) or {},
        "datetime": datetime,
        "extract_media": extract_media,
        "h1_turn_meta_lines": task_binding_module.h1_turn_meta_lines,
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: "",
        "resolve_task_binding": resolve_task_binding,
        "restore_h1_binding": task_binding_module.restore_h1_binding,
        "set_h1_dispatch_scope": task_binding_module.set_h1_dispatch_scope,
        "should_process_message": lambda *args, **kwargs: True,
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self, *, enabled=True, send_success=True, gateway_profile=None):
        self.name = "dingtalk"
        self.config = SimpleNamespace(extra={"natural_task_intake": enabled})
        self._allowed_users = set()
        self._mention_patterns = []
        self._dedup = SimpleNamespace(is_duplicate=lambda message_id: False)
        self._message_contexts = {}
        self._done_emoji_fired = set()
        self._session_webhooks = {}
        self._session_store = object()
        self._gateway_profile = gateway_profile
        self.events = []
        self.sent = []
        self.send_success = send_success

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
        message_id = f"outbound-{len(self.sent) + 1}"
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
                "message_id": message_id,
            }
        )
        return SimpleNamespace(success=self.send_success, message_id=message_id)


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


def make_quoted_message(text, *, replied_msg_id, quoted_text):
    message = make_message(text)
    message.text = SimpleNamespace(
        content=text,
        extensions={
            "repliedMsg": {
                "msgId": replied_msg_id,
                "msgType": "text",
                "content": quoted_text,
            }
        },
    )
    return message


def _fake_honest_failure(flag):
    """System-python 无 gateway 包：按 kit 惯例注入替身（用完恢复）。"""
    honest = types.ModuleType("gateway.honest_failure")
    honest.is_failure_receipt = lambda: flag
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    old_gateway = sys.modules.get("gateway")
    old_honest = sys.modules.get("gateway.honest_failure")
    sys.modules["gateway"] = gateway
    sys.modules["gateway.honest_failure"] = honest

    def restore():
        if old_gateway is None:
            sys.modules.pop("gateway", None)
        else:
            sys.modules["gateway"] = old_gateway
        if old_honest is None:
            sys.modules.pop("gateway.honest_failure", None)
        else:
            sys.modules["gateway.honest_failure"] = old_honest

    return restore


class DingTalkThinGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.on_message = staticmethod(load_on_message())

    def run_message(
        self,
        text,
        *,
        enabled=True,
        send_success=True,
        gateway_profile=None,
        materialize_result=None,
        materialize_raises=False,
        drive=None,
        on_message=None,
    ):
        materialize_calls = []

        def fake_materialize(session_store, source):
            materialize_calls.append({"session_store": session_store, "source": source})
            if materialize_raises:
                raise RuntimeError("task-selection state unavailable")
            return source if materialize_result is None else materialize_result

        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        task_intake = types.ModuleType("gateway.task_intake")
        task_intake.materialize_current_task_source = fake_materialize
        old_gateway = sys.modules.get("gateway")
        old_task_intake = sys.modules.get("gateway.task_intake")
        sys.modules["gateway"] = gateway
        sys.modules["gateway.task_intake"] = task_intake
        try:
            adapter = FakeAdapter(
                enabled=enabled,
                send_success=send_success,
                gateway_profile=gateway_profile,
            )
            adapter.materialize_calls = materialize_calls
            handler = on_message or self.on_message
            if drive is None:
                asyncio.run(handler(adapter, make_message(text)))
            else:
                asyncio.run(drive(adapter))
        finally:
            if old_gateway is None:
                sys.modules.pop("gateway", None)
            else:
                sys.modules["gateway"] = old_gateway
            if old_task_intake is None:
                sys.modules.pop("gateway.task_intake", None)
            else:
                sys.modules["gateway.task_intake"] = old_task_intake
        return adapter, materialize_calls

    # -- feature flag / structural short-circuits ----------------------------

    def test_feature_off_is_byte_compatible_and_never_materializes(self):
        adapter, calls = self.run_message("普通聊天", enabled=False)

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("普通聊天", adapter.events[0].text)
        self.assertIsNone(adapter.events[0].source.board_slug)

    def test_missing_sender_skipped_log_only_no_receipt(self):
        """V2: anonymous messages are skipped with only a log — no refusal
        receipt machinery anymore, and nothing reaches the agent."""

        def drive(adapter):
            message = make_message("在吗")
            message.sender_id = ""
            message.sender_staff_id = ""
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("在吗", drive=drive)

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual([], adapter.events)

    def test_missing_sender_media_also_skipped(self):
        media_on_message = load_on_message(with_media=True)

        def drive(adapter):
            message = make_message("看这个截图")
            message.sender_id = ""
            message.sender_staff_id = ""
            return media_on_message(adapter, message)

        adapter, calls = self.run_message(
            "看这个截图", drive=drive, on_message=media_on_message
        )

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual([], adapter.events)

    def test_slash_command_skips_materialize_and_meta(self):
        adapter, calls = self.run_message("/new")

        self.assertEqual([], calls)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("/new", adapter.events[0].text)
        self.assertNotIn("消息编号", adapter.events[0].text)

    def test_raw_binding_short_circuits_without_materialize(self):
        adapter, calls = self.run_message("#任务 agong/t_deadbeef 联系人为什么没显示")

        self.assertEqual([], calls)
        self.assertEqual(1, len(adapter.events))
        event = adapter.events[0]
        self.assertEqual("agong", event.source.board_slug)
        self.assertEqual("t_deadbeef", event.source.task_id)
        self.assertEqual("联系人为什么没显示", event.text)
        self.assertNotIn("消息编号", event.text)

    # -- per-message binding restore (D3/M3) ---------------------------------

    def test_every_message_restores_durable_binding(self):
        bound = FakeSource(
            chat_id="conversation-1",
            chat_type="group",
            user_id="sender-1",
            board_slug="agong",
            task_id="t_3852e516",
        )
        adapter, calls = self.run_message("这个索引为什么没数据", materialize_result=bound)

        self.assertEqual(1, len(calls))
        self.assertIs(bound, adapter.events[0].source)
        self.assertIn("这个索引为什么没数据", adapter.events[0].text)

    def test_materialize_failure_degrades_to_unbound_delivery(self):
        adapter, calls = self.run_message("继续排查", materialize_raises=True)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.sent)  # never an error receipt
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.board_slug)

    def test_media_restores_binding_and_keeps_attachment(self):
        media_on_message = load_on_message(with_media=True)
        bound = FakeSource(
            chat_id="conversation-1",
            user_id="sender-1",
            board_slug="agong",
            task_id="t_3852e516",
        )
        adapter, calls = self.run_message(
            "看这个截图", materialize_result=bound, on_message=media_on_message
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(adapter.events))
        self.assertIs(bound, adapter.events[0].source)
        self.assertEqual(["https://cdn.example.com/img.png"], adapter.events[0].media_urls)

    def test_media_materialize_failure_still_delivers_unbound(self):
        media_on_message = load_on_message(with_media=True)
        adapter, calls = self.run_message(
            "看这个截图", materialize_raises=True, on_message=media_on_message
        )

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.board_slug)

    def test_multiplex_profile_stamped_before_materialize(self):
        adapter, calls = self.run_message("普通聊天", gateway_profile="h1")

        self.assertEqual(1, len(calls))
        self.assertEqual("h1", calls[0]["source"].profile)

    # -- structural meta (§3.1) ----------------------------------------------

    def test_inbound_message_number_injected(self):
        adapter, _calls = self.run_message("帮我排查下这个报错")

        self.assertEqual(1, len(adapter.events))
        self.assertIn("[消息编号: incoming-1]", adapter.events[0].text)

    def test_delivery_receipt_injected_for_same_user(self):
        tb = load_task_binding_module()
        tb._h1_dispatch_scope.set(None)
        restore = _fake_honest_failure(False)

        def drive(adapter):
            try:
                source = adapter.build_source(
                    chat_id="conversation-1", chat_type="group",
                    user_id="sender-1", message_id="m-prev",
                )
                tb.set_h1_dispatch_scope(source=source, text="上一条", message_id="m-prev")
                tb.mark_h1_turn_delivered(
                    adapter, chat_id="conversation-1", message_id="out-prev-1"
                )
                tb._h1_dispatch_scope.set(None)
                return self.on_message(adapter, make_message("确认"))
            finally:
                restore()

        adapter, _calls = self.run_message("确认", drive=drive)

        self.assertEqual(1, len(adapter.events))
        self.assertIn("[你上一条回复已送达，编号: out-prev-1]", adapter.events[0].text)
        self.assertIn("[消息编号: incoming-1]", adapter.events[0].text)

    def test_delivery_receipt_not_injected_for_other_user(self):
        tb = load_task_binding_module()
        tb._h1_dispatch_scope.set(None)
        restore = _fake_honest_failure(False)

        def drive(adapter):
            try:
                source = adapter.build_source(
                    chat_id="conversation-1", chat_type="group",
                    user_id="sender-1", message_id="m-prev",
                )
                tb.set_h1_dispatch_scope(source=source, text="上一条", message_id="m-prev")
                tb.mark_h1_turn_delivered(
                    adapter, chat_id="conversation-1", message_id="out-prev-1"
                )
                tb._h1_dispatch_scope.set(None)
                message = make_message("我也说一句")
                message.sender_id = "sender-2"
                message.sender_staff_id = "staff-2"
                return self.on_message(adapter, message)
            finally:
                restore()

        adapter, _calls = self.run_message("我也说一句", drive=drive)

        self.assertEqual(1, len(adapter.events))
        self.assertNotIn("你上一条回复已送达", adapter.events[0].text)

    def test_failure_receipt_never_recorded(self):
        """R3 打标沿用：标记为失败回执的投递不置位、不记回执。"""
        tb = load_task_binding_module()
        tb._h1_dispatch_scope.set(None)
        adapter = FakeAdapter()
        source = adapter.build_source(
            chat_id="conversation-1", user_id="sender-1", message_id="m-1"
        )
        tb.set_h1_dispatch_scope(source=source, text="t", message_id="m-1")

        honest = types.ModuleType("gateway.honest_failure")
        honest.is_failure_receipt = lambda: True
        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        old_gateway = sys.modules.get("gateway")
        old_honest = sys.modules.get("gateway.honest_failure")
        sys.modules["gateway"] = gateway
        sys.modules["gateway.honest_failure"] = honest
        try:
            tb.mark_h1_turn_delivered(adapter, chat_id="conversation-1", message_id="out-err")
        finally:
            if old_gateway is None:
                sys.modules.pop("gateway", None)
            else:
                sys.modules["gateway"] = old_gateway
            if old_honest is None:
                sys.modules.pop("gateway.honest_failure", None)
            else:
                sys.modules["gateway.honest_failure"] = old_honest

        facts = getattr(adapter, "_h1_facts", {"replies": {}, "pending_delivery": {}})
        self.assertEqual({}, facts["replies"])
        self.assertEqual({}, facts["pending_delivery"])

    # -- reply context (D2) ---------------------------------------------------

    def test_quote_reply_without_original_still_clarifies(self):
        def drive(adapter):
            message = make_message("对")
            message._test_reply_kwargs = {
                "reply_to_message_id": "quoted-1",
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, _calls = self.run_message("对", drive=drive)

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[0]["content"])
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])

    def test_quote_reply_with_original_keeps_reply_context(self):
        def drive(adapter):
            message = make_quoted_message(
                "对", replied_msg_id="quoted-1", quoted_text="联系人任务的原消息"
            )
            message._test_reply_kwargs = {
                "reply_to_message_id": "quoted-1",
                "reply_to_text": "联系人任务的原消息",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, _calls = self.run_message("对", drive=drive)

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("quoted-1", adapter.events[0].reply_to_message_id)
        self.assertEqual("联系人任务的原消息", adapter.events[0].reply_to_text)


class ThinGateImportChainTest(unittest.TestCase):
    """C2② real-import smoke: the thin-gate symbols exist for real and the
    V1 machinery is really gone (no sys.modules doubles)."""

    def test_task_binding_real_import_surface(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "dingtalk_task_binding_real_import_smoke", BINDING_PATH
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        for symbol in (
            "materialize_natural_binding",
            "restore_h1_binding",
            "set_h1_dispatch_scope",
            "mark_h1_turn_delivered",
            "h1_turn_meta_lines",
            "is_h1_failure_receipt",
            "declare_h1_proposal",
            "get_h1_proposal",
            "try_consume_h1_proposal",
            "rollback_h1_consumption",
            "last_delivered_reply",
        ):
            self.assertTrue(hasattr(module, symbol), f"missing {symbol}")
        for removed in (
            "run_natural_intake_gate",
            "resolve_natural_intake",
            "validate_natural_intake_classifier_config",
            "install_h1_proposal_after_delivery",
            "slot_h1_proposal",
            "capture_h1_run_outcome",
            "record_h1_final_reply",
            "gate_bound_reply_kwargs",
            "build_intake_quote",
            "register_intake_prompt",
            "discard_undelivered_intake_pending",
        ):
            self.assertFalse(hasattr(module, removed), f"still present {removed}")

    def test_adapter_surface_and_thin_gate_method(self):
        """adapter 的真实现含薄闸门且无 V1 符号（AST 级，零依赖冒烟）。"""
        tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
        classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertIn("DingTalkAdapter", classes)
        source_text = ADAPTER_PATH.read_text(encoding="utf-8")
        for removed in (
            "run_natural_intake_gate",
            "validate_natural_intake_classifier_config",
            "capture_h1_run_outcome",
            "record_h1_final_reply",
            "gate_bound_reply_kwargs",
            "intake_bound",
        ):
            self.assertNotIn(removed, source_text)
        for present in (
            "restore_h1_binding",
            "set_h1_dispatch_scope",
            "h1_turn_meta_lines",
            "mark_h1_turn_delivered",
        ):
            self.assertIn(present, source_text)


if __name__ == "__main__":
    unittest.main()
