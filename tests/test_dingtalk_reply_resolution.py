"""Behavior contract for DingTalk reply-context forwarding."""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import re
import sys
import tempfile
import unittest
from unittest import mock
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
    def __init__(self, *, card_reply_store, send_success=True):
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
        self._card_reply_store = card_reply_store

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

    def run_message(
        self,
        message,
        *,
        send_success=True,
        remembered=None,
        remembered_webhook=None,
        remembered_chat="conversation-1",
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = self.reply_context.CardReplyStore(temp.name)
        if remembered:
            carrier_id, out_track_id, content = remembered
            response = SimpleNamespace(
                body=SimpleNamespace(
                    result=[SimpleNamespace(carrier_id=carrier_id, success=True)]
                )
            )
            self.assertTrue(
                store.remember_delivery(
                    remembered_chat, out_track_id, content, response
                )
            )
        if remembered_webhook:
            content, started_ms, finished_ms, response_body = remembered_webhook
            self.assertTrue(
                store.remember_webhook_delivery(
                    remembered_chat,
                    content,
                    started_ms,
                    finished_ms,
                    response_body,
                )
            )
        adapter = FakeAdapter(
            card_reply_store=store,
            send_success=send_success,
        )
        asyncio.run(self.on_message(adapter, message))
        return adapter

    def test_missing_original_clarifies_without_model_dispatch(self):
        adapter = self.run_message(
            make_message(replied={"msgId": "quoted-1", "msgType": "text"})
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual(
            self.reply_context._REPLY_ORIGINAL_CLARIFICATION,
            adapter.sent[0]["content"],
        )
        self.assertEqual("incoming-1", adapter.sent[0]["reply_to"])
        self.assertEqual(
            {"delivery_class": "business_error", "reply_recovery_prompt": True},
            adapter.sent[0]["metadata"],
        )

    def test_missing_original_stays_closed_when_clarification_send_fails(self):
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

    def test_interactive_card_original_is_recovered_by_exact_carrier_id(self):
        adapter = self.run_message(
            make_message(
                replied={"msgId": "carrier-27", "msgType": "interactiveCard"}
            ),
            remembered=("carrier-27", "hermes-track-19", "机器人卡片完整原文"),
        )

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("机器人卡片完整原文", adapter.events[0].reply_to_text)

    def test_interactive_card_original_is_recovered_by_webhook_response_message_id(self):
        adapter = self.run_message(
            make_message(
                replied={
                    "msgId": "webhook-message-27",
                    "msgType": "interactiveCard",
                    "createdAt": 1_800_000_000_100,
                }
            ),
            remembered_webhook=(
                "Webhook 机器人完整原文",
                1_800_000_000_000,
                1_800_000_000_200,
                {"messageId": "webhook-message-27"},
            ),
        )

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("Webhook 机器人完整原文", adapter.events[0].reply_to_text)

    def test_timestamp_candidate_requires_explicit_confirmation_before_dispatch(self):
        adapter = self.run_message(
            make_message(
                replied={
                    "msgId": "dingtalk-only-message-id",
                    "msgType": "interactiveCard",
                    "createdAt": 1_800_000_000_100,
                }
            ),
            remembered_webhook=(
                "按钉钉创建时间找回的原文",
                1_800_000_000_000,
                1_800_000_000_200,
                {"errcode": 0, "errmsg": "ok"},
            ),
        )

        self.assertEqual([], adapter.events)
        prompt = adapter.sent[0]["content"]
        self.assertIn("按钉钉创建时间找回的原文", prompt)
        self.assertIn("这个怎么处理", prompt)
        self.assertIn("尚未执行", prompt)
        command = re.search(r"确认引用 [0-9a-f]{8}", prompt).group(0)
        message = make_message()
        message.message_id = "confirmation-2"
        message.text.content = command
        asyncio.run(self.on_message(adapter, message))
        self.assertEqual(1, len(adapter.events))
        self.assertEqual("按钉钉创建时间找回的原文", adapter.events[0].reply_to_text)
        self.assertEqual("这个怎么处理", adapter.events[0].text)
        self.assertEqual("dingtalk-only-message-id", adapter.events[0].reply_to_message_id)
        message.message_id = "confirmation-3"
        asyncio.run(self.on_message(adapter, message))
        self.assertEqual(1, len(adapter.events), "a replay must not repeat the original request")
        self.assertIn("无效或已过期", adapter.sent[-1]["content"])

    def test_ambiguous_webhook_created_at_windows_fail_closed(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = self.reply_context.CardReplyStore(temp.name)
        for content in ("第一条", "第二条"):
            self.assertTrue(
                store.remember_webhook_delivery(
                    "conversation-1",
                    content,
                    1_800_000_000_000,
                    1_800_000_000_200,
                    {"errcode": 0},
                )
            )
        adapter = FakeAdapter(card_reply_store=store)
        asyncio.run(
            self.on_message(
                adapter,
                make_message(
                    replied={
                        "msgId": "unknown-message-id",
                        "msgType": "interactiveCard",
                        "createdAt": 1_800_000_000_100,
                    }
                ),
            )
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))

    def test_exact_time_window_can_only_propose_a_candidate(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = self.reply_context.CardReplyStore(temp.name)
        self.assertTrue(
            store.remember_webhook_delivery(
                "conversation-1",
                "精确时间窗原文",
                1_800_000_000_000,
                1_800_000_000_200,
                {"errcode": 0},
            )
        )
        self.assertTrue(
            store.remember_webhook_delivery(
                "conversation-1",
                "仅在误差范围内的另一条",
                1_800_000_003_000,
                1_800_000_003_200,
                {"errcode": 0},
            )
        )
        adapter = FakeAdapter(card_reply_store=store)
        asyncio.run(
            self.on_message(
                adapter,
                make_message(
                    replied={
                        "msgId": "unknown-message-id",
                        "msgType": "interactiveCard",
                        "createdAt": 1_800_000_000_100,
                    }
                ),
            )
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertIn("精确时间窗原文", adapter.sent[0]["content"])
        self.assertIn("尚未执行", adapter.sent[0]["content"])
        self.assertNotIn("仅在误差范围内的另一条", adapter.sent[0]["content"])

    def test_webhook_created_at_lookup_is_isolated_by_chat(self):
        adapter = self.run_message(
            make_message(
                replied={
                    "msgId": "unknown-message-id",
                    "msgType": "interactiveCard",
                    "createdAt": 1_800_000_000_100,
                }
            ),
            remembered_webhook=(
                "别的群的 Webhook 原文",
                1_800_000_000_000,
                1_800_000_000_200,
                {"errcode": 0},
            ),
            remembered_chat="conversation-2",
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))

    def test_unknown_interactive_card_still_clarifies_without_dispatch(self):
        adapter = self.run_message(
            make_message(
                replied={"msgId": "unknown-carrier", "msgType": "interactiveCard"}
            )
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))
        self.assertEqual(
            self.reply_context._REPLY_ORIGINAL_CLARIFICATION,
            adapter.sent[0]["content"],
        )

    def test_interactive_card_lookup_is_isolated_by_chat(self):
        adapter = self.run_message(
            make_message(
                replied={"msgId": "carrier-27", "msgType": "interactiveCard"}
            ),
            remembered=("carrier-27", "hermes-track-19", "别的群的卡片原文"),
            remembered_chat="conversation-2",
        )

        self.assertEqual([], adapter.events)
        self.assertEqual(1, len(adapter.sent))

    def test_non_reply_message_is_unchanged(self):
        adapter = self.run_message(make_message())

        self.assertEqual([], adapter.sent)
        self.assertEqual(1, len(adapter.events))
        self.assertFalse(hasattr(adapter.events[0], "reply_to_text"))

    def test_malformed_confirmation_never_reaches_model(self):
        for text in ("确认引用", "确认引用 oops", "确认引用 abcdef12 再发布另一篇"):
            with self.subTest(text=text):
                message = make_message()
                message.text.content = text
                adapter = self.run_message(message)
                self.assertEqual([], adapter.events)
                self.assertIn("无效或已过期", adapter.sent[0]["content"])

    def test_candidate_send_failure_never_dispatches_original_request(self):
        adapter = self.run_message(
            make_message(replied={"msgId": "unknown", "msgType": "interactivecard",
                                  "createdAt": 1_800_000_000_100}),
            remembered_webhook=("候选内容", 1_800_000_000_000, 1_800_000_000_200, {"errcode": 0}),
            send_success=False,
        )
        self.assertEqual([], adapter.events)
        self.assertIn("尚未执行", adapter.sent[0]["content"])

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


class CardReplyStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reply_context = load_reply_context()

    @staticmethod
    def delivery_response(*items):
        return SimpleNamespace(body=SimpleNamespace(result=list(items)))

    def test_extracts_only_successful_carrier_ids_from_sdk_shape(self):
        response = self.delivery_response(
            SimpleNamespace(carrier_id="carrier-a", success=True),
            SimpleNamespace(carrier_id="carrier-failed", success=False),
            {"carrierId": "carrier-b", "success": True},
        )

        self.assertEqual(
            ["carrier-a", "carrier-b"],
            self.reply_context._delivery_carrier_ids(response),
        )

    def test_mapping_survives_reopen_and_tracks_latest_card_content(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            response = self.delivery_response(
                SimpleNamespace(carrier_id="carrier-27", success=True)
            )

            with self.assertLogs(self.reply_context.logger, level="INFO") as logs:
                self.assertTrue(
                    store.remember_delivery(
                        "conversation-1", "hermes-track-19", "初始内容", response
                    )
                )
                self.assertTrue(
                    store.update_content(
                        "conversation-1", "hermes-track-19", "最终完整内容"
                    )
                )

                reopened = self.reply_context.CardReplyStore(state_dir)
                message = make_message(
                    replied={"msgId": "carrier-27", "msgType": "interactiveCard"}
                )
                self.assertEqual(
                    "最终完整内容",
                    reopened.resolve_message("conversation-1", message),
                )
            safe_log = "\n".join(logs.output)
            self.assertIn("carrier_count=1", safe_log)
            self.assertIn("lookup=hit", safe_log)
            self.assertNotIn("carrier-27", safe_log)
            self.assertNotIn("最终完整内容", safe_log)
            self.assertIsNone(reopened.resolve("conversation-2", "carrier-27"))
            self.assertIsNone(reopened.resolve("conversation-1", "hermes-track-19"))

    def test_failed_delivery_does_not_create_a_mapping(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            response = self.delivery_response(
                SimpleNamespace(carrier_id="carrier-failed", success=False)
            )

            self.assertFalse(
                store.remember_delivery(
                    "conversation-1", "hermes-track-19", "内容", response
                )
            )
            self.assertIsNone(store.resolve("conversation-1", "carrier-failed"))

    def test_webhook_time_window_mapping_survives_reopen_without_logging_content(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            with self.assertLogs(self.reply_context.logger, level="INFO") as logs:
                self.assertTrue(
                    store.remember_webhook_delivery(
                        "conversation-1",
                        "Webhook 持久化原文",
                        1_800_000_000_000,
                        1_800_000_000_200,
                        {"errcode": 0, "errmsg": "ok"},
                    )
                )
                reopened = self.reply_context.CardReplyStore(state_dir)
                message = make_message(
                    replied={
                        "msgId": "dingtalk-generated-id",
                        "msgType": "interactiveCard",
                        "createdAt": 1_800_000_000_100,
                    }
                )
                self.assertIsNone(reopened.resolve_message("conversation-1", message))
                prompt = reopened.propose_confirmation("conversation-1", "staff-1", message, "请继续")
                self.assertIn("Webhook 持久化原文", prompt)
            safe_log = "\n".join(logs.output)
            self.assertIn("exact_id_count=0", safe_log)
            self.assertIn("source=created_at", safe_log)
            self.assertNotIn("Webhook 持久化原文", safe_log)

    def test_confirmation_is_scoped_expiring_single_use_and_restart_safe(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            store.remember_webhook_delivery("chat-1", "完整候选", 1_800_000_000_000,
                                            1_800_000_000_200, {"errcode": 0})
            message = make_message(replied={"msgId": "unknown", "msgType": "interactivecard",
                                            "createdAt": 1_800_000_000_100})
            prompt = store.propose_confirmation("chat-1", "sender-1", message, "可以发布了")
            token = re.search(r"确认引用 ([0-9a-f]{8})", prompt).group(1)
            reopened = self.reply_context.CardReplyStore(state_dir)
            self.assertIsNone(reopened.consume_confirmation("chat-2", "sender-1", token))
            self.assertIsNone(reopened.consume_confirmation("chat-1", "sender-2", token))
            self.assertIsNone(reopened.consume_confirmation("chat-1", "sender-1", "wrong"))
            with mock.patch.object(self.reply_context.time, "time", return_value=10**12):
                self.assertIsNone(reopened.consume_confirmation("chat-1", "sender-1", token))
            self.assertEqual(
                {"request_text": "可以发布了", "reply_to_message_id": "unknown", "reply_to_text": "完整候选"},
                reopened.consume_confirmation("chat-1", "sender-1", token),
            )
            self.assertIsNone(reopened.consume_confirmation("chat-1", "sender-1", token))

    def test_new_candidate_replaces_old_confirmation_and_requires_full_preview(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            store.remember_webhook_delivery("chat", "候选", 1_800_000_000_000,
                                            1_800_000_000_200, {"errcode": 0})
            message = make_message(replied={"msgId": "unknown", "msgType": "interactivecard",
                                            "createdAt": 1_800_000_000_100})
            first = store.propose_confirmation("chat", "sender", message, "旧请求")
            second = store.propose_confirmation("chat", "sender", message, "新请求")
            first_token = re.search(r"确认引用 ([0-9a-f]{8})", first).group(1)
            second_token = re.search(r"确认引用 ([0-9a-f]{8})", second).group(1)
            self.assertIsNone(store.consume_confirmation("chat", "sender", first_token))
            self.assertEqual("新请求", store.consume_confirmation("chat", "sender", second_token)["request_text"])
            self.assertIsNone(store.propose_confirmation("chat", "sender", message, "x" * 20000))
            self.assertIsNone(store.propose_confirmation("chat", "", message, "请求"))

    def test_exact_webhook_id_collision_does_not_pick_arbitrary_content(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            for content in ("甲", "乙"):
                store.remember_webhook_delivery("chat", content, 1_800_000_000_000,
                                                1_800_000_000_200, {"messageId": "duplicate"})
            message = make_message(replied={"msgId": "duplicate", "msgType": "interactivecard"})
            self.assertIsNone(store.resolve_message("chat", message))

    def test_storage_failure_never_returns_saved_or_resolved_success(self):
        with tempfile.TemporaryDirectory() as state_dir:
            not_directory = Path(state_dir) / "file"
            not_directory.write_text("not a directory")
            store = self.reply_context.CardReplyStore(str(not_directory))
            self.assertFalse(store.remember_delivery("chat", "track", "正文", self.delivery_response(
                SimpleNamespace(carrier_id="carrier", success=True))))
            self.assertFalse(store.remember_webhook_delivery("chat", "正文", 1, 2, {"errcode": 0}))
            self.assertFalse(store.invalidate_content("chat", "track"))
            self.assertIsNone(store.resolve("chat", "carrier"))
            self.assertIsNone(store.consume_confirmation("chat", "sender", "abcdef12"))

    def test_invalidation_survives_restart_when_final_save_fails(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            store.remember_delivery("chat", "track", "旧正文", self.delivery_response(
                SimpleNamespace(carrier_id="carrier", success=True)))
            self.assertTrue(store.invalidate_content("chat", "track"))
            with mock.patch.object(store, "_connect", side_effect=OSError("disk full")):
                self.assertFalse(store.update_content("chat", "track", "新正文"))
            self.assertIsNone(self.reply_context.CardReplyStore(state_dir).resolve("chat", "carrier"))

    def test_invalid_callback_timestamps_are_not_candidates(self):
        for value in (float("nan"), float("inf"), "Infinity", None, -1):
            self.assertIsNone(self.reply_context._coerce_epoch_ms(value))

    def test_default_store_uses_persistent_hermes_home(self):
        with tempfile.TemporaryDirectory() as hermes_home:
            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_HOME": hermes_home,
                    "DINGTALK_KIT_STATE_DIR": "",
                },
            ):
                store = self.reply_context.CardReplyStore()
                self.assertEqual(
                    str(Path(hermes_home) / "dingtalk-kit" / "dingtalk_card_replies.db"),
                    store._db_path(),
                )

if __name__ == "__main__":
    unittest.main()
