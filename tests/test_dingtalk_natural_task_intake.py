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


def load_reply_context_module():
    """Load the real reply_context.py quote-metadata extractors.

    Also registered under its plain module name: task_binding.py loads
    standalone in these tests, so its lazy ``from reply_context import ...``
    fallback inside ``build_intake_quote`` resolves here.
    """
    import importlib.util

    name = "dingtalk_reply_context_for_intake_uut"
    spec = importlib.util.spec_from_file_location(name, REPLY_CONTEXT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    sys.modules.setdefault("reply_context", module)
    return module


def load_task_binding_module():
    """Load the real task_binding.py helpers under test.

    The offload (``asyncio.to_thread``), profile stamping, prompt registry,
    and intake gate under test live in that module, so the seam tests must
    drive the real implementations — with ``gateway.task_intake`` still
    patched to a controllable fake.
    """
    import importlib.util

    load_reply_context_module()
    name = "dingtalk_task_binding_for_intake_uut"
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
        "is_user_allowed": lambda *args, **kwargs: True,
        "logger": FakeLogger(),
        "mention_meta_line": lambda *args, **kwargs: "",
        "resolve_task_binding": resolve_task_binding,
        "run_natural_intake_gate": task_binding_module.run_natural_intake_gate,
        "should_process_message": lambda *args, **kwargs: True,
        "timezone": timezone,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="generated-message-id")),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_on_message"]


class FakeAdapter:
    def __init__(self, *, enabled, send_success=True, gateway_profile=None, send_raises=False):
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
        self._intake_prompt_msgs = {}
        self.events = []
        self.sent = []
        self.send_success = send_success
        self.send_raises = send_raises

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
        if self.send_raises:
            raise RuntimeError("send exploded")
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
        send_raises=False,
        gateway_profile=None,
        resolver_override=None,
        materialize_result=None,
        materialize_raises=False,
        discard=None,
        drive=None,
        on_message=None,
    ):
        calls = []
        materialize_calls = []

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

        def recording_materialize(session_store, source):
            materialize_calls.append(
                {"session_store": session_store, "source": source}
            )
            if materialize_raises:
                raise RuntimeError("task-selection state unavailable")
            # The real Core helper returns the source unchanged when there is
            # no current binding (task_intake.py:1339).
            return source if materialize_result is None else materialize_result

        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        task_intake = types.ModuleType("gateway.task_intake")
        task_intake.resolve_natural_task_intake = resolver_override or resolver
        task_intake.materialize_current_task_source = recording_materialize
        if discard is not None:
            task_intake.discard_natural_intake_pending = discard
        old_gateway = sys.modules.get("gateway")
        old_task_intake = sys.modules.get("gateway.task_intake")
        sys.modules["gateway"] = gateway
        sys.modules["gateway.task_intake"] = task_intake
        try:
            adapter = FakeAdapter(
                enabled=enabled,
                send_success=send_success,
                send_raises=send_raises,
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

    # -- R9 #3 (D6): quote-aware confirmation --------------------------------

    @staticmethod
    def _prompt_result(
        operation_id="op-1", phase="choose_task", target_digest="digest-1"
    ):
        """A Core reply that installs a pending and sends a confirmation prompt."""
        return SimpleNamespace(
            action="reply_without_agent",
            source=None,
            text="",
            reply_text="我找到 2 个任务，请选择 1 或 2。",
            prompt_operation_id=operation_id,
            prompt_phase=phase,
            prompt_target_digest=target_digest,
        )

    def test_plain_message_passes_quote_none(self):
        """No repliedMsg => quote=None: today's behavior is unchanged."""
        adapter, calls = self.run_message("普通聊天")

        self.assertEqual(1, len(calls))
        self.assertIn("quote", calls[0]["kwargs"])
        self.assertIsNone(calls[0]["kwargs"]["quote"])

    def test_prompt_send_registers_msg_id(self):
        """A delivered confirmation prompt is registered chat_id -> msg_id -> triple."""
        adapter, calls = self.run_message("联系人为什么没显示", result=self._prompt_result())

        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(adapter.sent))
        sent_message_id = adapter.sent[0]["message_id"]
        entries = adapter._intake_prompt_msgs.get("conversation-1")
        self.assertIsNotNone(entries)
        self.assertIn(sent_message_id, entries)
        operation_id, phase, target_digest, expires_at = entries[sent_message_id]
        self.assertEqual("op-1", operation_id)
        self.assertEqual("choose_task", phase)
        self.assertEqual("digest-1", target_digest)
        self.assertGreater(expires_at, time.monotonic())

    def test_prompt_registration_requires_successful_send_and_operation(self):
        """Failed sends, missing operation ids, and error actions register nothing."""
        # (a) the prompt send failed — nothing to quote later.
        adapter, _ = self.run_message(
            "联系人为什么没显示", result=self._prompt_result(), send_success=False
        )
        self.assertEqual({}, adapter._intake_prompt_msgs)
        # (b) a legacy Core result without prompt_operation_id.
        legacy = SimpleNamespace(
            action="reply_without_agent", source=None, text="", reply_text="请选择 1 或 2。"
        )
        adapter, _ = self.run_message("联系人为什么没显示", result=legacy)
        self.assertEqual({}, adapter._intake_prompt_msgs)
        # (c) error_without_agent is never a confirmation prompt.
        error = SimpleNamespace(
            action="error_without_agent",
            source=None,
            text="",
            reply_text="任务账本暂时无法安全检索。",
            prompt_operation_id="op-err",
            prompt_phase="choose_task",
            prompt_target_digest="digest-err",
        )
        adapter, _ = self.run_message("联系人为什么没显示", result=error)
        self.assertEqual({}, adapter._intake_prompt_msgs)

    def test_prompt_registration_requires_complete_triple(self):
        """R2 C1: an operation id without phase/digest must not be registered —
        a quote of that prompt simply stays unauthenticated (fail closed)."""
        incomplete = SimpleNamespace(
            action="reply_without_agent",
            source=None,
            text="",
            reply_text="请选择 1 或 2。",
            prompt_operation_id="op-1",
        )
        adapter, _ = self.run_message("联系人为什么没显示", result=incomplete)
        self.assertEqual({}, adapter._intake_prompt_msgs)

        partial = SimpleNamespace(
            action="reply_without_agent",
            source=None,
            text="",
            reply_text="请选择 1 或 2。",
            prompt_operation_id="op-1",
            prompt_phase="choose_task",
        )
        adapter, _ = self.run_message("联系人为什么没显示", result=partial)
        self.assertEqual({}, adapter._intake_prompt_msgs)

    def test_quote_of_registered_prompt_passes_matched_op_and_confirm_consumed(self):
        """G3: quoting the bot's confirmation prompt with '确认' confirms normally.

        The resolver must receive an authenticated quote whose matched triple
        is the pending operation's; when Core consumes the control word
        (bound_source + control_consumed) the first bound turn reaches the
        gateway and the T1 reply clarification stays silent — no regression
        of the plain-text confirm flow.
        """
        seen = []
        bound = {
            "chat_id": "conversation-1",
            "chat_type": "group",
            "user_id": "sender-1",
            "message_id": "incoming-2",
            "board_slug": "agong",
            "task_id": "t_3852e516",
        }
        staged_results = iter(
            [
                self._prompt_result(),
                SimpleNamespace(
                    action="bound_source",
                    source=bound,
                    text="联系人为什么没显示",
                    reply_text=None,
                    control_consumed=True,
                ),
            ]
        )

        def staged_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append({"text": message_text, "quote": kwargs.get("quote")})
            return next(staged_results)

        def drive(adapter):
            async def flow():
                await self.on_message(adapter, make_message("联系人为什么没显示"))
                confirm = make_message("确认")
                confirm.message_id = "incoming-2"
                confirm.text.extensions = {
                    "repliedMsg": {"msgId": "outbound-1", "msgType": "markdown"}
                }
                await self.on_message(adapter, confirm)

            return flow()

        adapter, calls = self.run_message("确认", resolver_override=staged_resolver, drive=drive)

        self.assertEqual(2, len(seen))
        self.assertIsNone(seen[0]["quote"])
        self.assertEqual(
            {
                "replied_message_id": "outbound-1",
                "authenticated": True,
                "matched": {
                    "operation_id": "op-1",
                    "phase": "choose_task",
                    "target_digest": "digest-1",
                },
                "quoted_text": None,
            },
            seen[1]["quote"],
        )
        # Exactly one send: the confirmation prompt. No T1 clarification.
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("我找到 2 个任务，请选择 1 或 2。", adapter.sent[0]["content"])
        # The consumed confirm delivered the bound first turn to the gateway.
        self.assertEqual(1, len(adapter.events))
        self.assertIs(bound, adapter.events[0].source)
        self.assertEqual("联系人为什么没显示", adapter.events[0].text)

    def test_quote_of_registered_prompt_carries_quoted_text(self):
        """The replied original (when DingTalk delivers it) rides along as quoted_text."""
        seen = []

        def staged_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            if len(seen) == 1:
                return self._prompt_result()
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            async def flow():
                await self.on_message(adapter, make_message("联系人为什么没显示"))
                quoted = make_message("这是指的哪个任务？")
                quoted.message_id = "incoming-2"
                quoted.text.extensions = {
                    "repliedMsg": {
                        "msgId": "outbound-1",
                        "msgType": "markdown",
                        "content": {"text": "我找到 2 个任务，请选择 1 或 2。"},
                    }
                }
                await self.on_message(adapter, quoted)

            return flow()

        adapter, calls = self.run_message("这是指的哪个任务？", resolver_override=staged_resolver, drive=drive)

        self.assertEqual(2, len(seen))
        self.assertEqual(
            {
                "replied_message_id": "outbound-1",
                "authenticated": True,
                "matched": {
                    "operation_id": "op-1",
                    "phase": "choose_task",
                    "target_digest": "digest-1",
                },
                "quoted_text": "我找到 2 个任务，请选择 1 或 2。",
            },
            seen[1],
        )

    def test_quote_of_foreign_message_passes_none_and_clarifies(self):
        """G3: quoting someone else's message with '确认' is consumed 0 times.

        The registry has no entry for the quoted id, so the resolver sees an
        unauthenticated quote (matched=None); Core answers pass_through
        (pending left untouched) and the adapter falls back to the T1 reply
        clarification.
        """
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            # What the new Core returns for a control word whose quote is not
            # authenticated for the pending operation: refuse to consume.
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            message = make_message("确认")
            message.text.extensions = {
                "repliedMsg": {"msgId": "foreign-message-1", "msgType": "text"}
            }
            message._test_reply_kwargs = {
                "reply_to_message_id": "foreign-message-1",
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("确认", resolver_override=recording_resolver, drive=drive)

        self.assertEqual(1, len(seen))
        self.assertEqual("foreign-message-1", seen[0]["replied_message_id"])
        self.assertFalse(seen[0]["authenticated"])
        self.assertIsNone(seen[0]["matched"])
        # Consumed 0 times: nothing reaches the gateway, the T1 clarification fires.
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[0]["content"])
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])

    def test_quote_of_other_chat_prompt_passes_none(self):
        """A prompt registered in chat A must not match a quote sent in chat B."""
        seen = []

        def staged_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            if len(seen) == 1:
                return self._prompt_result()
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            async def flow():
                await self.on_message(adapter, make_message("联系人为什么没显示"))
                other_chat = make_message("确认")
                other_chat.message_id = "incoming-2"
                other_chat.conversation_id = "conversation-2"
                other_chat.text.extensions = {
                    "repliedMsg": {"msgId": "outbound-1", "msgType": "markdown"}
                }
                other_chat._test_reply_kwargs = {
                    "reply_to_message_id": "outbound-1",
                    "reply_to_text": "reply unavailable",
                    "reply_to_is_own_message": False,
                }
                await self.on_message(adapter, other_chat)

            return flow()

        adapter, calls = self.run_message("确认", resolver_override=staged_resolver, drive=drive)

        self.assertEqual(2, len(seen))
        self.assertFalse(seen[1]["authenticated"])
        self.assertIsNone(seen[1]["matched"])
        # Chat B got the T1 clarification; chat A's registry entry is intact.
        self.assertEqual([], adapter.events)
        self.assertEqual(2, len(adapter.sent))  # chat A's prompt + chat B's clarification
        self.assertEqual("conversation-2", adapter.sent[1]["chat_id"])
        self.assertEqual("reply clarification", adapter.sent[1]["content"])
        self.assertIn("outbound-1", adapter._intake_prompt_msgs["conversation-1"])

    def test_quote_of_expired_prompt_passes_none_and_clarifies(self):
        """An expired registry entry is a miss (and purged), never a match."""
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            adapter._intake_prompt_msgs["conversation-1"] = {
                "stale-prompt": ("op-old", "choose_task", "digest-old", time.monotonic() - 1.0)
            }
            message = make_message("确认")
            message.text.extensions = {
                "repliedMsg": {"msgId": "stale-prompt", "msgType": "markdown"}
            }
            message._test_reply_kwargs = {
                "reply_to_message_id": "stale-prompt",
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("确认", resolver_override=recording_resolver, drive=drive)

        self.assertEqual(1, len(seen))
        self.assertFalse(seen[0]["authenticated"])
        self.assertIsNone(seen[0]["matched"])
        # Purged on lookup — the emptied chat key is removed too (M11).
        self.assertNotIn("conversation-1", adapter._intake_prompt_msgs)
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[0]["content"])

    def test_prompt_registry_bounded_and_expires(self):
        """Unit: per-chat FIFO cap + TTL, with an injected monotonic clock."""
        module = load_task_binding_module()
        register = module.register_intake_prompt
        match = module.match_intake_prompt
        ttl = module._INTAKE_PROMPT_REGISTRY_TTL_SECONDS
        max_per_chat = module._INTAKE_PROMPT_REGISTRY_MAX_PER_CHAT

        # The registry must outlive the Core pending TTL (900s) it points at.
        self.assertGreater(ttl, 900.0)

        registry = {}
        for index in range(max_per_chat + 3):
            register(
                registry, "chat-1", f"msg-{index}", f"op-{index}",
                "choose_task", f"digest-{index}", now=1000.0 + index,
            )
        entries = registry["chat-1"]
        self.assertEqual(max_per_chat, len(entries))
        self.assertNotIn("msg-2", entries)  # oldest evicted first (FIFO)
        self.assertIn(f"msg-{max_per_chat + 2}", entries)
        self.assertEqual(
            {
                "operation_id": f"op-{max_per_chat + 2}",
                "phase": "choose_task",
                "target_digest": f"digest-{max_per_chat + 2}",
            },
            match(registry, "chat-1", f"msg-{max_per_chat + 2}", now=2000.0),
        )
        # Chat isolation: an id from chat-1 is unknown in chat-2.
        self.assertIsNone(match(registry, "chat-2", f"msg-{max_per_chat + 2}", now=2000.0))

        # TTL: a hit before expiry matches; at expiry it misses and is purged.
        register(registry, "chat-2", "msg-x", "op-x", "confirm_create", "digest-x", now=1000.0)
        self.assertEqual(
            {
                "operation_id": "op-x",
                "phase": "confirm_create",
                "target_digest": "digest-x",
            },
            match(registry, "chat-2", "msg-x", now=1000.0 + ttl - 1),
        )
        self.assertIsNone(match(registry, "chat-2", "msg-x", now=1000.0 + ttl))
        # The purged chat left no empty outer key behind (M11).
        self.assertNotIn("chat-2", registry)

    def test_prompt_registry_eviction_logs_warning(self):
        """M11: FIFO eviction of the oldest entry is observable."""
        module = load_task_binding_module()
        register = module.register_intake_prompt
        max_per_chat = module._INTAKE_PROMPT_REGISTRY_MAX_PER_CHAT

        registry = {}
        for index in range(max_per_chat):
            register(
                registry, "chat-1", f"msg-{index}", f"op-{index}",
                "choose_task", f"digest-{index}", now=1000.0 + index,
            )
        with self.assertLogs(module.logger, level="WARNING") as captured:
            register(
                registry, "chat-1", "msg-new", "op-new",
                "choose_task", "digest-new", now=2000.0,
            )
        self.assertTrue(
            any("evicted" in line for line in captured.output),
            f"expected an eviction warning, got: {captured.output}",
        )
        self.assertNotIn("msg-0", registry["chat-1"])
        self.assertIn("msg-new", registry["chat-1"])

    def test_prompt_registry_expiry_purge_drops_empty_chat_key(self):
        """M11: expiring the last entry of a chat removes the outer chat key."""
        module = load_task_binding_module()
        register = module.register_intake_prompt
        ttl = module._INTAKE_PROMPT_REGISTRY_TTL_SECONDS

        registry = {}
        register(registry, "chat-1", "msg-a", "op-a", "choose_task", "digest-a", now=1000.0)
        register(registry, "chat-1", "msg-b", "op-b", "choose_task", "digest-b", now=1000.0)
        # Registering a fresh entry past the TTL purges both stale ones and
        # leaves only the new entry under the surviving chat key.
        register(registry, "chat-1", "msg-c", "op-c", "choose_task", "digest-c", now=1000.0 + ttl + 1)
        self.assertEqual(["msg-c"], list(registry["chat-1"]))
        # A later register after that entry too expired must not leave an
        # empty ``{}`` behind: the chat key is dropped and recreated.
        registry2 = {}
        register(registry2, "chat-9", "msg-z", "op-z", "choose_task", "digest-z", now=1000.0)
        self.assertIsNone(
            module.match_intake_prompt(registry2, "chat-9", "msg-z", now=1000.0 + ttl)
        )
        self.assertNotIn("chat-9", registry2)

    # -- R2 C1/C2: quote authentication hardening -----------------------------

    def test_quote_of_old_phase_prompt_delivers_its_registered_triple(self):
        """R2 C1: quoting an OLD phase's prompt must not confirm the new phase.

        The same operation reuses its id across phase transitions; the Kit's
        job is to deliver the quoted prompt's OWN registered triple, so Core
        can see it differs from the current pending triple and refuse to
        consume (pass_through → T1 clarification).
        """
        seen = []

        def staged_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            if len(seen) <= 2:
                # Two prompts for the same operation id at two phases.
                return (
                    self._prompt_result(phase="choose_task", target_digest="digest-old")
                    if len(seen) == 1
                    else self._prompt_result(phase="confirm_create", target_digest="digest-new")
                )
            # Contract-Core: matched triple != current pending triple => do
            # not consume the control word.
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            async def flow():
                await self.on_message(adapter, make_message("联系人为什么没显示"))
                phase_two = make_message("2")
                phase_two.message_id = "incoming-2"
                await self.on_message(adapter, phase_two)
                confirm = make_message("确认")
                confirm.message_id = "incoming-3"
                # The user quotes the FIRST (old-phase) prompt, not the second.
                confirm.text.extensions = {
                    "repliedMsg": {"msgId": "outbound-1", "msgType": "markdown"}
                }
                confirm._test_reply_kwargs = {
                    "reply_to_message_id": "outbound-1",
                    "reply_to_text": "reply unavailable",
                    "reply_to_is_own_message": False,
                }
                await self.on_message(adapter, confirm)

            return flow()

        adapter, calls = self.run_message("确认", resolver_override=staged_resolver, drive=drive)

        self.assertEqual(3, len(seen))
        self.assertEqual(
            {
                "replied_message_id": "outbound-1",
                "authenticated": True,
                "matched": {
                    "operation_id": "op-1",
                    "phase": "choose_task",
                    "target_digest": "digest-old",
                },
                "quoted_text": None,
            },
            seen[2],
        )
        # Core refused to consume: nothing reached the gateway, the T1
        # clarification fired after the two prompts.
        self.assertEqual([], adapter.events)
        self.assertEqual(3, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[2]["content"])
        # Both prompts stay registered under their own triples.
        entries = adapter._intake_prompt_msgs["conversation-1"]
        self.assertEqual("digest-old", entries["outbound-1"][2])
        self.assertEqual("digest-new", entries["outbound-2"][2])

    def test_quote_missing_msgid_is_unauthenticated_and_clarifies(self):
        """R2 C2: a repliedMsg WITHOUT msgId used to degrade to 'not a quote'.

        It must now stay an explicit unauthenticated quote (fail closed):
        ``replied_message_id=None``, ``authenticated=False``, ``matched=None``
        — while ``quoted_text`` still rides along for display only. A control
        word is then never consumed (Core pass_through → T1 clarification).
        """
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            message = make_message("确认")
            message.text.extensions = {
                "repliedMsg": {
                    "msgType": "text",
                    "content": {"text": "我找到 2 个任务，请选择 1 或 2。"},
                }
            }
            message._test_reply_kwargs = {
                "reply_to_text": "reply unavailable",
                "reply_to_is_own_message": False,
            }
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("确认", resolver_override=recording_resolver, drive=drive)

        self.assertEqual(1, len(seen))
        self.assertEqual(
            {
                "replied_message_id": None,
                "authenticated": False,
                "matched": None,
                "quoted_text": "我找到 2 个任务，请选择 1 或 2。",
            },
            seen[0],
        )
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[0]["content"])

    def test_quote_of_webhook_prompt_is_unauthenticated_despite_quoted_text(self):
        """R2 C2: a webhook-delivered prompt can never authenticate a quote.

        The webhook send path returns a locally synthesized uuid as
        ``SendResult.message_id``; DingTalk assigns its own msgId to the
        delivered message, so a later quote of that prompt references an id
        the registry never saw. Even with the prompt text echoed back as
        ``quoted_text``, the quote stays unauthenticated — the text is
        display-only, never an authorization signal.
        """
        seen = []

        def staged_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            if len(seen) == 1:
                return self._prompt_result()
            # Contract-Core: unauthenticated quote => do not consume.
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            async def flow():
                await self.on_message(adapter, make_message("联系人为什么没显示"))
                # Simulate the production webhook layout: the registry holds
                # the synthetic local id, while the quote carries the real
                # platform msgId of the same visible prompt.
                synthetic = adapter._intake_prompt_msgs["conversation-1"].pop("outbound-1")
                adapter._intake_prompt_msgs["conversation-1"]["a1b2c3d4e5f6"] = synthetic
                confirm = make_message("确认")
                confirm.message_id = "incoming-2"
                confirm.text.extensions = {
                    "repliedMsg": {
                        "msgId": "dingtalk-platform-msg-9",
                        "msgType": "markdown",
                        "content": {"text": "我找到 2 个任务，请选择 1 或 2。"},
                    }
                }
                confirm._test_reply_kwargs = {
                    "reply_to_message_id": "dingtalk-platform-msg-9",
                    "reply_to_text": "reply unavailable",
                    "reply_to_is_own_message": False,
                }
                await self.on_message(adapter, confirm)

            return flow()

        adapter, calls = self.run_message("确认", resolver_override=staged_resolver, drive=drive)

        self.assertEqual(2, len(seen))
        self.assertEqual(
            {
                "replied_message_id": "dingtalk-platform-msg-9",
                "authenticated": False,
                "matched": None,
                "quoted_text": "我找到 2 个任务，请选择 1 或 2。",
            },
            seen[1],
        )
        self.assertEqual([], adapter.events)
        self.assertEqual(2, len(adapter.sent))
        self.assertEqual("reply clarification", adapter.sent[1]["content"])

    def test_missing_inbound_message_id_gets_honest_error_and_stops(self):
        """R5 I1: an intake-eligible text without a platform msgId is refused
        loudly — honest error sent, resolver never called, and the message
        never reaches the tool-wielding main agent."""
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append({"text": message_text, "request_id": request_id})
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            message = make_message("联系人为什么没显示")
            message.message_id = None  # platform delivered no stable id
            return self.on_message(adapter, message)

        adapter, calls = self.run_message(
            "联系人为什么没显示", resolver_override=recording_resolver, drive=drive
        )

        self.assertEqual([], seen)
        self.assertEqual([], adapter.events)  # NOT handed to the agent
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("无法安全处理任务", adapter.sent[0]["content"])

    def test_blank_inbound_message_id_gets_honest_error_and_stops(self):
        """R5 I1: a whitespace-only inbound msgId is refused the same way."""
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(message_text)
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            message = make_message("联系人为什么没显示")
            message.message_id = "   "
            return self.on_message(adapter, message)

        adapter, calls = self.run_message(
            "联系人为什么没显示", resolver_override=recording_resolver, drive=drive
        )

        self.assertEqual([], seen)
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("无法安全处理任务", adapter.sent[0]["content"])

    # -- R2 C3: startup classifier-config validation --------------------------

    @staticmethod
    def _install_fake_intent_classifier(*, validate=None, read=None):
        """Install a fake ``gateway.intent_classifier``; return a restore hook."""
        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        intent_classifier = types.ModuleType("gateway.intent_classifier")
        if validate is not None:
            intent_classifier.validate_intent_classifier_config = validate
        if read is not None:
            intent_classifier.read_intent_classification_config = read
        keys = ("gateway", "gateway.intent_classifier")
        saved = {key: sys.modules.get(key) for key in keys}
        sys.modules["gateway"] = gateway
        sys.modules["gateway.intent_classifier"] = intent_classifier

        def restore():
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value

        return restore

    def test_classifier_config_startup_check_logs_critical_on_malformed(self):
        """R2 C3: feature on + malformed config => CRITICAL log, never raises.

        The feature is not silently disabled and the probe never aborts the
        connect — the log is the ops-visible signal; Core keeps failing
        honestly per message.
        """
        module = load_task_binding_module()

        class IntentConfigError(Exception):
            pass

        def validate(raw):
            raise IntentConfigError("auxiliary.intent_classification 配置缺失或不是对象")

        restore = self._install_fake_intent_classifier(validate=validate, read=lambda: None)
        try:
            with self.assertLogs(module.logger, level="CRITICAL") as captured:
                module.validate_natural_intake_classifier_config(
                    {"natural_task_intake": True}, "dingtalk"
                )
        finally:
            restore()

        self.assertTrue(
            any("intent_classification" in line for line in captured.output),
            f"expected the config key in the CRITICAL log, got: {captured.output}",
        )

    def test_classifier_config_startup_check_skips_old_core_and_feature_off(self):
        """R2 C3 compat: an old Core without the helper is skipped gracefully;
        feature off never validates; a valid config stays silent."""
        module = load_task_binding_module()

        # (a) old Core: the module exists but has no validate function.
        restore = self._install_fake_intent_classifier()
        try:
            with self.assertNoLogs(module.logger, level="CRITICAL"):
                module.validate_natural_intake_classifier_config(
                    {"natural_task_intake": True}, "dingtalk"
                )
        finally:
            restore()

        calls = []

        # (b) feature off: the validator must not even be consulted.
        restore = self._install_fake_intent_classifier(
            validate=lambda raw: calls.append(raw), read=lambda: None
        )
        try:
            module.validate_natural_intake_classifier_config({}, "dingtalk")
            module.validate_natural_intake_classifier_config(
                {"natural_task_intake": False}, "dingtalk"
            )
        finally:
            restore()
        self.assertEqual([], calls)

        # (c) valid config: validated with the raw value, no CRITICAL.
        restore = self._install_fake_intent_classifier(
            validate=lambda raw: calls.append(raw) or raw,
            read=lambda: {"provider": "openai", "model": "gpt-x"},
        )
        try:
            with self.assertNoLogs(module.logger, level="CRITICAL"):
                module.validate_natural_intake_classifier_config(
                    {"natural_task_intake": True}, "dingtalk"
                )
        finally:
            restore()
        self.assertEqual([{"provider": "openai", "model": "gpt-x"}], calls)

    def test_connect_runs_classifier_config_startup_check(self):
        """Wiring (R2 C3): adapter.connect() must invoke the startup check."""
        tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
        connect = next(
            item
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter"
            for item in node.body
            if isinstance(item, ast.AsyncFunctionDef) and item.name == "connect"
        )
        called = {
            node.func.id
            for node in ast.walk(connect)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("validate_natural_intake_classifier_config", called)

    # -- R3 #2/#3: malformed repliedMsg + direct quote clarification -----------

    def test_malformed_replied_msg_is_explicit_unauthenticated_quote(self):
        """R3 #2: a present-but-malformed repliedMsg must never read as 'no quote'.

        None / non-dict / empty-dict values all yield an explicit
        authenticated=False quote; the contract-Core answers with the quote
        clarification action, so a control word can never be consumed as a
        plain unquoted confirm.
        """
        module = load_task_binding_module()
        for malformed in (None, "garbage-string", {}):
            with self.subTest(malformed=repr(malformed)):
                seen = []

                def clarifying_resolver(session_store, source, message_text, request_id, **kwargs):
                    seen.append(kwargs.get("quote"))
                    return SimpleNamespace(
                        action=module.QUOTE_CLARIFICATION_ACTION,
                        source=None,
                        text="",
                        reply_text="我无法确认你引用的内容，请说明是哪条提示。",
                    )

                def drive(adapter):
                    message = make_message("确认")
                    message.text.extensions = {"repliedMsg": malformed}
                    return self.on_message(adapter, message)

                adapter, calls = self.run_message(
                    "确认", resolver_override=clarifying_resolver, drive=drive
                )

                self.assertEqual(1, len(seen))
                self.assertEqual(
                    {
                        "replied_message_id": None,
                        "authenticated": False,
                        "matched": None,
                        "quoted_text": None,
                    },
                    seen[0],
                )
                self.assertEqual([], adapter.events)
                self.assertEqual(1, len(adapter.sent))
                self.assertIn("引用", adapter.sent[0]["content"])
                self.assertEqual({}, adapter._intake_prompt_msgs)

    def test_unauthenticated_control_quote_directly_clarified_by_core_action(self):
        """R3 #3: an unauthenticated quote on a control intent is answered by
        the gate itself — even when the quoted text IS available (the old
        reply-context path would have let it reach the agent)."""
        seen = []
        module = load_task_binding_module()

        def clarifying_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            return SimpleNamespace(
                action=module.QUOTE_CLARIFICATION_ACTION,
                source=None,
                text="",
                reply_text="我无法确认你引用的是哪条提示，请直接回复任务编号或重新选择。",
            )

        def drive(adapter):
            message = make_message("确认")
            message.text.extensions = {
                "repliedMsg": {
                    "msgId": "foreign-message-1",
                    "msgType": "text",
                    "content": {"text": "别人的消息原文"},
                }
            }
            # No _test_reply_kwargs sentinel: a pass_through would sail
            # straight into the agent with full reply context.
            return self.on_message(adapter, message)

        adapter, calls = self.run_message("确认", resolver_override=clarifying_resolver, drive=drive)

        self.assertEqual(1, len(seen))
        self.assertFalse(seen[0]["authenticated"])
        self.assertIsNone(seen[0]["matched"])
        self.assertEqual("别人的消息原文", seen[0]["quoted_text"])
        self.assertEqual([], adapter.events)  # never reached the agent
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("引用", adapter.sent[0]["content"])
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])
        # No live pending => the clarification carries no triple => no registration.
        self.assertEqual({}, adapter._intake_prompt_msgs)

    def test_quote_clarification_with_live_pending_registers_and_authenticates(self):
        """R3 #3 closed loop: a quote clarification restating the live
        pending's prompt carries its full triple, so the delivered
        clarification registers — quoting IT with '确认' then authenticates
        and consumes exactly like quoting the original prompt."""
        module = load_task_binding_module()
        seen = []
        bound = {
            "chat_id": "conversation-1",
            "chat_type": "group",
            "user_id": "sender-1",
            "message_id": "incoming-3",
            "board_slug": "agong",
            "task_id": "t_3852e516",
        }
        triple = {
            "prompt_operation_id": "op-1",
            "prompt_phase": "choose_task",
            "prompt_target_digest": "digest-1",
        }
        staged_results = iter(
            [
                self._prompt_result(),  # outbound-1: the original prompt
                # Contract-Core: unauthenticated quote + live pending =>
                # quote_clarification restating the pending with its triple.
                SimpleNamespace(
                    action=module.QUOTE_CLARIFICATION_ACTION,
                    source=None,
                    text="",
                    reply_text="你引用的消息未能核验为当前待确认的提示。\n当前待确认：\n我找到 2 个任务，请选择 1 或 2。",
                    **triple,
                ),
                SimpleNamespace(
                    action="bound_source",
                    source=bound,
                    text="联系人为什么没显示",
                    reply_text=None,
                    control_consumed=True,
                ),
            ]
        )

        def staged_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(kwargs.get("quote"))
            return next(staged_results)

        def drive(adapter):
            async def flow():
                await self.on_message(adapter, make_message("联系人为什么没显示"))
                bad_quote = make_message("确认")
                bad_quote.message_id = "incoming-2"
                bad_quote.text.extensions = {
                    "repliedMsg": {"msgId": "foreign-message-1", "msgType": "text"}
                }
                await self.on_message(adapter, bad_quote)
                good_quote = make_message("确认")
                good_quote.message_id = "incoming-3"
                # The user now quotes the CLARIFICATION message (outbound-2).
                good_quote.text.extensions = {
                    "repliedMsg": {"msgId": "outbound-2", "msgType": "markdown"}
                }
                await self.on_message(adapter, good_quote)

            return flow()

        adapter, calls = self.run_message("确认", resolver_override=staged_resolver, drive=drive)

        self.assertEqual(3, len(seen))
        self.assertIsNone(seen[0])
        self.assertFalse(seen[1]["authenticated"])
        # The clarification was registered under its real outbound id with the
        # live pending's triple — quoting it authenticates.
        self.assertEqual(
            {
                "replied_message_id": "outbound-2",
                "authenticated": True,
                "matched": {
                    "operation_id": "op-1",
                    "phase": "choose_task",
                    "target_digest": "digest-1",
                },
                "quoted_text": None,
            },
            seen[2],
        )
        entries = adapter._intake_prompt_msgs["conversation-1"]
        self.assertEqual("digest-1", entries["outbound-1"][2])
        self.assertEqual("digest-1", entries["outbound-2"][2])
        # The consumed confirm delivered the bound first turn to the gateway.
        self.assertEqual(1, len(adapter.events))
        self.assertIs(bound, adapter.events[0].source)

    # -- R3 #7: registry global sweep + outer chat cap --------------------------

    def test_prompt_registry_global_sweep_reclaims_idle_chats(self):
        """R3 #7: register/match sweep expired entries in EVERY chat, so idle
        chats are reclaimed even when they never see another prompt/quote."""
        module = load_task_binding_module()
        register = module.register_intake_prompt
        match = module.match_intake_prompt
        ttl = module._INTAKE_PROMPT_REGISTRY_TTL_SECONDS

        registry = {}
        register(registry, "chat-idle", "msg-a", "op-a", "choose_task", "digest-a", now=1000.0)
        register(registry, "chat-live", "msg-b", "op-b", "choose_task", "digest-b", now=1000.0)
        # Registering into a third chat past the TTL sweeps both idle chats.
        register(registry, "chat-new", "msg-c", "op-c", "choose_task", "digest-c", now=1000.0 + ttl + 1)
        self.assertNotIn("chat-idle", registry)
        self.assertNotIn("chat-live", registry)
        self.assertEqual(["msg-c"], list(registry["chat-new"]))

        # An expired-hit match sweeps globally too, not just its own chat.
        register(registry, "chat-x", "msg-x", "op-x", "choose_task", "digest-x", now=3000.0)
        register(registry, "chat-y", "msg-y", "op-y", "choose_task", "digest-y", now=3000.0)
        self.assertIsNone(match(registry, "chat-x", "msg-x", now=3000.0 + ttl))
        self.assertNotIn("chat-x", registry)
        self.assertNotIn("chat-y", registry)  # swept although never touched

    def test_prompt_registry_match_sweeps_globally_on_hit_and_miss(self):
        """R4 M5: every match lookup sweeps expired entries in EVERY chat —
        a live hit or an ordinary miss reclaims other chats' stale entries,
        not just an expired hit in the looked-up chat."""
        module = load_task_binding_module()
        register = module.register_intake_prompt
        match = module.match_intake_prompt
        ttl = module._INTAKE_PROMPT_REGISTRY_TTL_SECONDS
        now = 1000.0 + ttl + 1  # entries registered at 1000.0 are expired

        # Live hit path: the stale chats are reclaimed as a side effect.
        # (chat-live registers BEFORE the stale entries expire, so its own
        # register-sweep cannot pre-clean them — the match must do it.)
        registry = {}
        register(registry, "chat-stale-1", "msg-a", "op-a", "choose_task", "digest-a", now=1000.0)
        register(registry, "chat-stale-2", "msg-b", "op-b", "choose_task", "digest-b", now=1000.0)
        register(registry, "chat-live", "msg-c", "op-c", "choose_task", "digest-c", now=2000.0)
        self.assertEqual(
            {"operation_id": "op-c", "phase": "choose_task", "target_digest": "digest-c"},
            match(registry, "chat-live", "msg-c", now=now),
        )
        self.assertNotIn("chat-stale-1", registry)
        self.assertNotIn("chat-stale-2", registry)

        # Ordinary miss path: same global sweep, live entry untouched.
        registry = {}
        register(registry, "chat-stale-1", "msg-a", "op-a", "choose_task", "digest-a", now=1000.0)
        register(registry, "chat-stale-2", "msg-b", "op-b", "choose_task", "digest-b", now=1000.0)
        register(registry, "chat-live", "msg-c", "op-c", "choose_task", "digest-c", now=2000.0)
        self.assertIsNone(match(registry, "chat-live", "unknown-msg", now=now))
        self.assertNotIn("chat-stale-1", registry)
        self.assertNotIn("chat-stale-2", registry)
        self.assertIn("msg-c", registry["chat-live"])

    def test_prompt_registry_match_sweeps_even_with_empty_arguments(self):
        """R5 M5: the global sweep runs before the empty-parameter check —
        even a degenerate match call reclaims expired entries."""
        module = load_task_binding_module()
        registry = {}
        module.register_intake_prompt(
            registry, "chat-1", "msg-a", "op-a", "choose_task", "digest-a", now=1000.0
        )
        ttl = module._INTAKE_PROMPT_REGISTRY_TTL_SECONDS
        self.assertIsNone(module.match_intake_prompt(registry, "", None, now=1000.0 + ttl))
        self.assertNotIn("chat-1", registry)

    def test_prompt_registry_chat_cap_evicts_oldest_chat_with_warning(self):
        """R3 #7: the outer chat map is capped; evicting a chat logs a warning."""
        module = load_task_binding_module()
        register = module.register_intake_prompt
        max_chats = module._INTAKE_PROMPT_REGISTRY_MAX_CHATS
        self.assertEqual(256, max_chats)

        registry = {}
        for index in range(max_chats):
            register(
                registry, f"chat-{index}", f"msg-{index}", f"op-{index}",
                "choose_task", f"digest-{index}", now=1000.0,
            )
        self.assertEqual(max_chats, len(registry))
        with self.assertLogs(module.logger, level="WARNING") as captured:
            register(
                registry, "chat-new", "msg-new", "op-new",
                "choose_task", "digest-new", now=1000.0,
            )
        self.assertEqual(max_chats, len(registry))
        self.assertNotIn("chat-0", registry)  # oldest chat evicted first
        self.assertIn("chat-new", registry)
        self.assertTrue(
            any("evicted" in line and "chat" in line for line in captured.output),
            f"expected a chat-eviction warning, got: {captured.output}",
        )

    # -- R5 I2: undelivered prompt withdraws the pending -----------------------

    def test_prompt_send_raise_discards_pending_and_stays_handled(self):
        """R6 I2: a prompt send that RAISES is a failed delivery too — the
        pending is withdrawn with the full triple, the exception never
        escapes the gate (no _safe_on_message swallow), handled=True."""
        discards = []

        def fake_discard(session_store, source, *, operation_id, phase, target_digest, now=None):
            discards.append(
                {
                    "session_store": session_store,
                    "source": source,
                    "operation_id": operation_id,
                    "phase": phase,
                    "target_digest": target_digest,
                }
            )
            return True

        adapter, calls = self.run_message(
            "联系人为什么没显示",
            result=self._prompt_result(),
            send_raises=True,
            discard=fake_discard,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(discards))
        self.assertEqual("op-1", discards[0]["operation_id"])
        self.assertEqual("choose_task", discards[0]["phase"])
        self.assertEqual("digest-1", discards[0]["target_digest"])
        self.assertEqual({}, adapter._intake_prompt_msgs)  # never registered
        self.assertEqual([], adapter.events)  # handled; agent never saw it
        self.assertEqual([], adapter.sent)  # the send raised before recording

    def test_identity_refusal_send_raise_only_warns_and_stays_handled(self):
        """R6 I2: a refusal receipt that raises is logged, never re-sent, and
        the exception never escapes the gate (message still handled)."""
        module = load_task_binding_module()

        def drive(adapter):
            message = make_message("联系人为什么没显示")
            message.message_id = None
            return self.on_message(adapter, message)

        with self.assertLogs(module.logger, level="WARNING") as captured:
            adapter, calls = self.run_message(
                "联系人为什么没显示", send_raises=True, drive=drive
            )

        self.assertEqual([], calls)
        self.assertEqual([], adapter.events)
        self.assertEqual([], adapter.sent)
        self.assertTrue(
            any("refusal" in line for line in captured.output),
            f"expected a refusal-receipt warning, got: {captured.output}",
        )
        # The stamp is NOT set on failure — the next message retries.
        self.assertEqual({}, adapter._intake_refusal_stamps)

    # -- R6 M4: refusal receipt cooldown ---------------------------------------

    def test_identity_refusal_is_cooled_down_per_chat_and_reason(self):
        """R6 M4: a malformed-message storm gets ONE refusal per chat+reason
        per window; repeats are still handled (zero resolver calls, zero
        agent delivery) without resending; after expiry it sends again."""
        module = load_task_binding_module()
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(message_text)
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            async def flow():
                for _ in range(3):
                    message = make_message("联系人为什么没显示")
                    message.message_id = None  # each callback: fresh dedup UUID
                    await self.on_message(adapter, message)

            return flow()

        adapter, calls = self.run_message(
            "联系人为什么没显示", resolver_override=recording_resolver, drive=drive
        )

        self.assertEqual([], seen)
        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))  # one receipt for three messages
        self.assertIn("无法安全处理任务", adapter.sent[0]["content"])

        # A different reason (same chat) and a different chat (same reason)
        # each get their own receipt.
        def drive_variants(adapter):
            async def flow():
                no_sender = make_message("联系人为什么没显示")
                no_sender.sender_id = "   "
                no_sender.sender_staff_id = ""
                await self.on_message(adapter, no_sender)
                other_chat = make_message("联系人为什么没显示")
                other_chat.message_id = None
                other_chat.conversation_id = "conversation-2"
                await self.on_message(adapter, other_chat)

            return flow()

        adapter, calls = self.run_message("联系人为什么没显示", drive=drive_variants)
        self.assertEqual([], adapter.events)
        self.assertEqual(2, len(adapter.sent))
        self.assertEqual({"conversation-1", "conversation-2"}, {s["chat_id"] for s in adapter.sent})

        # After the cooldown window the receipt is sent again.
        cooldown = module._INTAKE_REFUSAL_COOLDOWN_SECONDS

        def drive_after_expiry(adapter):
            async def flow():
                await self.on_message(adapter, _msgidless())
                for key in list(adapter._intake_refusal_stamps):
                    adapter._intake_refusal_stamps[key] -= cooldown + 1
                await self.on_message(adapter, _msgidless())

            return flow()

        def _msgidless():
            message = make_message("联系人为什么没显示")
            message.message_id = None
            return message

        adapter, calls = self.run_message("联系人为什么没显示", drive=drive_after_expiry)
        self.assertEqual([], adapter.events)
        self.assertEqual(2, len(adapter.sent))

    # -- R5 I2: undelivered prompt withdraws the pending -----------------------

    def test_prompt_send_failure_discards_pending_with_triple(self):
        """R5 I2: a failed prompt/clarification send withdraws the pending via
        Core's discard with the full triple; nothing is registered and the
        flow stays handled (agent never involved)."""
        module = load_task_binding_module()
        results = {
            "reply_without_agent": self._prompt_result(),
            "quote_clarification": SimpleNamespace(
                action=module.QUOTE_CLARIFICATION_ACTION,
                source=None,
                text="",
                reply_text="你引用的消息未能核验为当前待确认的提示。",
                prompt_operation_id="op-1",
                prompt_phase="choose_task",
                prompt_target_digest="digest-1",
            ),
        }
        for name, result in results.items():
            with self.subTest(action=name):
                discards = []

                def fake_discard(session_store, source, *, operation_id, phase, target_digest, now=None):
                    discards.append(
                        {
                            "session_store": session_store,
                            "source": source,
                            "operation_id": operation_id,
                            "phase": phase,
                            "target_digest": target_digest,
                        }
                    )
                    return True

                adapter, calls = self.run_message(
                    "联系人为什么没显示",
                    result=result,
                    send_success=False,
                    discard=fake_discard,
                )

                self.assertEqual(1, len(calls))
                self.assertEqual(1, len(discards))
                self.assertEqual("op-1", discards[0]["operation_id"])
                self.assertEqual("choose_task", discards[0]["phase"])
                self.assertEqual("digest-1", discards[0]["target_digest"])
                self.assertIs(adapter._session_store, discards[0]["session_store"])
                self.assertIsNotNone(discards[0]["source"])
                self.assertEqual({}, adapter._intake_prompt_msgs)  # never registered
                self.assertEqual([], adapter.events)  # handled; agent never saw it
                self.assertEqual(1, len(adapter.sent))  # only the failed prompt send

    def test_prompt_send_failure_discard_problems_only_warn(self):
        """R5 I2: discard returning False / raising / missing on an old Core
        degrades to a warning — never a crash, never an extra send."""
        module = load_task_binding_module()

        # (a) discard returns False (the pending moved on): warning, no crash.
        with self.assertLogs(module.logger, level="WARNING") as captured:
            adapter, _ = self.run_message(
                "联系人为什么没显示",
                result=self._prompt_result(),
                send_success=False,
                discard=lambda *args, **kwargs: False,
            )
        self.assertTrue(
            any("undelivered" in line for line in captured.output),
            f"expected an undelivered-pending warning, got: {captured.output}",
        )
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual([], adapter.events)

        # (b) discard raises: warning (with exc_info), no crash.
        def raising_discard(*args, **kwargs):
            raise RuntimeError("cas failed")

        with self.assertLogs(module.logger, level="WARNING") as captured:
            adapter, _ = self.run_message(
                "联系人为什么没显示",
                result=self._prompt_result(),
                send_success=False,
                discard=raising_discard,
            )
        self.assertTrue(
            any("undelivered" in line for line in captured.output),
            f"expected an undelivered-pending warning, got: {captured.output}",
        )

        # (c) old Core without the helper: skipped gracefully with a warning.
        with self.assertLogs(module.logger, level="WARNING") as captured:
            adapter, _ = self.run_message(
                "联系人为什么没显示",
                result=self._prompt_result(),
                send_success=False,
            )
        self.assertTrue(
            any("undelivered" in line for line in captured.output),
            f"expected an undelivered-pending warning, got: {captured.output}",
        )
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual([], adapter.events)

    # -- R9 #6 (D7): media keeps the current binding --------------------------

    def test_media_with_current_binding_restores_source(self):
        """G4: a bound user's media recovers its task context before the gateway."""
        bound_source = FakeSource(
            chat_id="conversation-1",
            chat_type="group",
            user_id="sender-1",
            board_slug="agong",
            task_id="t_deadbeef",
        )
        adapter, calls = self.run_message(
            "看这个截图",
            materialize_result=bound_source,
            on_message=load_on_message(with_media=True),
        )

        self.assertEqual([], calls)  # resolver never called — media never enters intake
        self.assertEqual(1, len(adapter.materialize_calls))
        self.assertIsNone(adapter.materialize_calls[0]["source"].board_slug)
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIs(bound_source, adapter.events[0].source)
        self.assertEqual("agong", adapter.events[0].source.board_slug)
        self.assertEqual("t_deadbeef", adapter.events[0].source.task_id)
        self.assertEqual(["https://cdn.example.com/img.png"], adapter.events[0].media_urls)

    def test_media_without_binding_stays_unbound(self):
        """G4: no current binding => the media event is delivered as before."""
        adapter, calls = self.run_message(
            "看这个截图",
            on_message=load_on_message(with_media=True),
        )

        self.assertEqual([], calls)
        self.assertEqual(1, len(adapter.materialize_calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.board_slug)
        self.assertIsNone(adapter.events[0].source.task_id)

    def test_media_materialize_failure_still_delivers_unbound(self):
        """G4: a materialize failure is logged and never drops the media."""
        adapter, calls = self.run_message(
            "看这个截图",
            materialize_raises=True,
            on_message=load_on_message(with_media=True),
        )

        self.assertEqual([], calls)
        self.assertEqual(1, len(adapter.materialize_calls))
        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.board_slug)
        self.assertEqual(["https://cdn.example.com/img.png"], adapter.events[0].media_urls)

    # -- R9 #9 (D10): missing stable sender id fails closed -------------------

    def test_missing_sender_ids_gets_honest_error_and_stops(self):
        """#9 + R5 I1: no stable sender identity => intake never runs AND the
        intake-eligible text is refused with an honest error instead of
        reaching the tool-wielding main agent."""
        seen = []

        def recording_resolver(session_store, source, message_text, request_id, **kwargs):
            seen.append(message_text)
            return SimpleNamespace(
                action="pass_through",
                source=source,
                text=message_text,
                reply_text=None,
            )

        def drive(adapter):
            message = make_message("联系人为什么没显示")
            message.sender_id = "   "  # whitespace-only is not a stable id either
            message.sender_staff_id = ""
            return self.on_message(adapter, message)

        adapter, calls = self.run_message(
            "联系人为什么没显示", resolver_override=recording_resolver, drive=drive
        )

        self.assertEqual([], seen)
        self.assertEqual([], adapter.events)  # NOT handed to the agent
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("无法安全处理任务", adapter.sent[0]["content"])

    def test_missing_sender_ids_skips_media_materialize_but_reaches_gateway(self):
        """#9 + R5 I1: an anonymous MEDIA message is not intake-eligible — it
        skips binding restore and is delivered unbound, with no honest-error
        send (behavior for non-eligible messages is unchanged)."""
        media_on_message = load_on_message(with_media=True)

        def drive(adapter):
            message = make_message("看这个截图")
            message.sender_id = ""
            message.sender_staff_id = ""
            return media_on_message(adapter, message)

        adapter, calls = self.run_message(
            "看这个截图",
            drive=drive,
            on_message=media_on_message,
        )

        self.assertEqual([], calls)
        self.assertEqual([], adapter.materialize_calls)
        self.assertEqual([], adapter.sent)  # media is not refused — it flows on
        self.assertEqual(1, len(adapter.events))
        self.assertIsNone(adapter.events[0].source.board_slug)


if __name__ == "__main__":
    unittest.main()
