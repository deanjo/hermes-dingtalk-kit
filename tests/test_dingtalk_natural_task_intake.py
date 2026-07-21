"""Behavior contract for the DingTalk-to-Core natural task intake seam."""

from __future__ import annotations

import ast
import asyncio
import copy
import re
import sys
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"
BINDING_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/task_binding.py"
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


def load_session_key_namespace():
    """Load Core's real ``_session_key_namespace`` rule from the overlay."""
    tree = ast.parse(SESSION_PATH.read_text(encoding="utf-8"), filename=str(SESSION_PATH))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_session_key_namespace":
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {"Optional": Optional}
            exec(compile(module, str(SESSION_PATH), "exec"), namespace)
            return namespace["_session_key_namespace"]
    raise RuntimeError("_session_key_namespace not found in overlay session.py")


def load_natural_intake():
    """Load the real ``resolve_natural_intake`` helper from task_binding.py.

    The offload (``asyncio.to_thread``) and profile stamping under test live
    in that helper, so the seam tests must drive the real implementation —
    with ``gateway.task_intake`` still patched to a controllable fake.
    """
    import importlib.util

    name = "dingtalk_task_binding_for_intake_uut"
    spec = importlib.util.spec_from_file_location(name, BINDING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.resolve_natural_intake


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
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: "",
        "resolve_natural_intake": load_natural_intake(),
        "resolve_task_binding": resolve_task_binding,
        "should_process_message": lambda *args, **kwargs: True,
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self, *, enabled, send_success=True, gateway_profile=None):
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
        gateway_profile=None,
        resolver_override=None,
        drive=None,
        on_message=None,
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
        task_intake.resolve_natural_task_intake = resolver_override or resolver
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
        return adapter, calls

    def test_feature_off_is_byte_compatible_and_never_calls_resolver(self):
        adapter, calls = self.run_message("普通聊天", enabled=False)

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("普通聊天", adapter.events[0].text)
        self.assertIsNone(adapter.events[0].source.board_slug)

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
        self.assertIsNone(adapter.events[0].source.board_slug)

    def test_quote_reply_confirm_consumed_by_intake_skips_reply_clarification(self):
        """E2E F1: a quote-replied confirm word that the intake resolver
        consumes (bound_source) must not be hijacked by the T1
        reply-original-unavailable clarification — the bound first turn
        reaches the gateway and no clarification is sent."""
        bound = {
            "chat_id": "conversation-1",
            "chat_type": "group",
            "user_id": "sender-1",
            "message_id": "incoming-1",
            "board_slug": "agong",
            "task_id": "t_3852e516",
        }
        result = SimpleNamespace(
            action="bound_source",
            source=bound,
            text="联系人为什么没显示",
            reply_text=None,
            control_consumed=True,
        )

        def drive(adapter):
            message = make_message("新建吧")
            message._test_reply_kwargs = {
                "reply_to_message_id": "quoted-1",
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("新建吧", result=result, drive=drive)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIs(bound, adapter.events[0].source)
        self.assertEqual("联系人为什么没显示", adapter.events[0].text)

    def test_quote_reply_pass_through_still_sends_reply_clarification(self):
        """Guard: when the resolver does NOT consume the message, the T1
        reply-original-unavailable clarification keeps its old behavior."""

        def drive(adapter):
            message = make_message("普通聊天")
            message._test_reply_kwargs = {
                "reply_to_message_id": "quoted-1",
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("普通聊天", drive=drive)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[0]["content"])
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])

    def test_current_task_quote_reply_without_original_still_clarifies(self):
        """Codex rework: an ordinary quote-reply routed to the current task
        (bound_source WITHOUT control_consumed) keeps the T1 clarification
        when the quoted original is unavailable."""
        bound = {
            "chat_id": "conversation-1",
            "chat_type": "group",
            "user_id": "sender-1",
            "message_id": "incoming-1",
            "board_slug": "agong",
            "task_id": "t_3852e516",
        }
        result = SimpleNamespace(
            action="bound_source",
            source=bound,
            text="对",
            reply_text=None,
            control_consumed=False,
        )

        def drive(adapter):
            message = make_message("对")
            message._test_reply_kwargs = {
                "reply_to_message_id": "quoted-1",
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("对", result=result, drive=drive)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[0]["content"])
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])

    def test_current_task_quote_reply_with_original_keeps_reply_context(self):
        """Codex rework: an ordinary quote-reply routed to the current task
        keeps its reply context so Core can inject the pointer."""
        bound = {
            "chat_id": "conversation-1",
            "chat_type": "group",
            "user_id": "sender-1",
            "message_id": "incoming-1",
            "board_slug": "agong",
            "task_id": "t_3852e516",
        }
        result = SimpleNamespace(
            action="bound_source",
            source=bound,
            text="对",
            reply_text=None,
            control_consumed=False,
        )

        def drive(adapter):
            message = make_message("对")
            message._test_reply_kwargs = {
                "reply_to_message_id": "quoted-1",
                "reply_to_text": "联系人任务的原消息",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("对", result=result, drive=drive)

        self.assertEqual(1, len(calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("quoted-1", adapter.events[0].reply_to_message_id)
        self.assertEqual("联系人任务的原消息", adapter.events[0].reply_to_text)

    def test_raw_binding_bypasses_natural_resolver_and_stays_per_message(self):
        adapter, calls = self.run_message(
            "#任务 agong/t_deadbeef 联系人为什么没显示"
        )

        self.assertEqual([], calls)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("agong", adapter.events[0].source.board_slug)
        self.assertEqual("t_deadbeef", adapter.events[0].source.task_id)
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

    def test_resolver_runs_off_the_event_loop(self):
        """A slow synchronous resolver must not stall the event loop.

        The Core resolver scans Kanban and waits on SQLite locks, so it must
        be offloaded (``asyncio.to_thread``). A 20ms tick scheduled next to a
        250ms resolver has to fire BEFORE the resolver returns; an inline
        synchronous call serializes them and the tick lands after.
        """
        resolver_ended_at = []
        tick_fired_at = []

        def slow_resolver(session_store, source, message_text, request_id, **kwargs):
            time.sleep(0.25)
            resolver_ended_at.append(time.monotonic())
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        async def drive(adapter):
            async def ticker():
                await asyncio.sleep(0.02)
                tick_fired_at.append(time.monotonic())

            await asyncio.gather(
                self.on_message(adapter, make_message("普通聊天")),
                ticker(),
            )

        adapter, calls = self.run_message(
            "普通聊天",
            resolver_override=slow_resolver,
            drive=drive,
        )

        self.assertEqual(1, len(resolver_ended_at))
        self.assertEqual(1, len(tick_fired_at))
        self.assertLess(
            tick_fired_at[0],
            resolver_ended_at[0],
            "event loop tick was blocked by the synchronous resolver",
        )
        # Return semantics are unchanged: pass_through still reaches the gateway.
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("普通聊天", adapter.events[0].text)

    def test_multiplex_profile_is_stamped_before_resolver_and_isolates_state(self):
        """The resolver must see the profile the gateway will key state on.

        The gateway stamps ``source.profile`` only after the adapter hands
        over the event, but the task-state key (``task_state_key`` →
        ``_resolve_profile_for_key``) reads it inside the resolver. The
        adapter therefore stamps the profile it was constructed under, so
        two multiplex profiles in the same chat never share task state.
        """
        namespace_of = load_session_key_namespace()
        seen_profiles = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen_profiles.append(source.profile)
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        for profile in ("default", "coder", None):
            adapter, calls = self.run_message(
                "普通聊天",
                gateway_profile=profile,
                resolver_override=recording_resolver,
            )
            self.assertEqual(1, len(adapter.events))

        self.assertEqual(["default", "coder", None], seen_profiles)
        # Core's real key rule: the two stamped profiles resolve to different
        # session-key namespaces, so their task-state keys cannot collide.
        self.assertNotEqual(namespace_of("default"), namespace_of("coder"))

    def test_media_only_message_bypasses_natural_resolver_and_reaches_gateway(self):
        """A media message with no text is not an (empty) task query.

        With natural intake enabled, ``text == ""`` + media previously fell
        into the resolver with an empty query and was answered with a
        task-selection reply, so the media never reached the gateway. Textless
        media must skip the resolver and continue the legacy media path.
        """
        adapter, calls = self.run_message(
            "",
            resolver_override=lambda session_store, source, message_text, request_id, **kwargs: (
                SimpleNamespace(
                    # What real Core returns for an empty query (task_intake.py:540).
                    action="reply_without_agent",
                    source=None,
                    text="",
                    reply_text="没有找到相关任务。要新建「Agong / 新任务」吗？回复“新建吧”。",
                )
            ),
            on_message=load_on_message(with_media=True),
        )

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual(["https://cdn.example.com/img.png"], adapter.events[0].media_urls)

    def test_media_with_text_bypasses_natural_resolver_and_keeps_both(self):
        """Text + media must not enter task intake either (v1 boundary).

        When the resolver asks for task selection (``reply_without_agent``),
        the adapter replies and returns before building the MessageEvent, so
        the attachment is lost — and Core's pending record has no media
        fields to replay it after confirmation. v1 therefore bypasses the
        resolver for ANY message with media; text and media both reach the
        gateway on the legacy path.
        """
        adapter, calls = self.run_message(
            "联系人为什么没显示",
            resolver_override=lambda session_store, source, message_text, request_id, **kwargs: (
                SimpleNamespace(
                    action="reply_without_agent",
                    source=None,
                    text="",
                    reply_text="我找到 2 个任务，请选择 1 或 2。",
                )
            ),
            on_message=load_on_message(with_media=True),
        )

        self.assertEqual([], calls)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("联系人为什么没显示", adapter.events[0].text)
        self.assertEqual(["https://cdn.example.com/img.png"], adapter.events[0].media_urls)


if __name__ == "__main__":
    unittest.main()
