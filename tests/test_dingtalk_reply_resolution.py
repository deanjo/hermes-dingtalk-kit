"""Behavior contract for DingTalk reply-context forwarding."""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
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
        "append_full_reply_text": reply_context.append_full_reply_text,
        "append_conversation_context": reply_context.append_conversation_context,
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
        success = self.send_success.pop(0) if isinstance(self.send_success, list) else self.send_success
        return SimpleNamespace(success=success)


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

    def test_missing_original_and_natural_words_reach_model(self):
        for words in ["继续，是原文", "别用这段，换成我刚说的", "确认引用 xyz", "为什么这样理解？", "不是", "嗯，可以，但把尾句删掉"]:
            message = make_message(replied={"msgId": "missing", "msgType": "text"})
            message.text.content = words
            adapter = self.run_message(message)
            self.assertEqual([], adapter.sent)
            self.assertEqual(1, len(adapter.events))
            event = adapter.events[0]
            self.assertTrue(event.text.startswith(words + "\n\n"))
            self.assertIn("引用原文未取得", event.text)
            self.assertIsNone(event.reply_to_text)
            self.assertFalse(event.allow_gateway_control)

    def test_complete_saved_original_overrides_callback_preview(self):
        original = "长原文。" * 6000 + "TAIL-731"
        message = make_message(replied={"msgId": "saved", "msgType": "text", "content": "预览"})
        adapter = self.run_message(message, remembered=("saved", "track", original))
        self.assertEqual(original, adapter.events[0].reply_to_text)
        self.assertIn(original, adapter.events[0].text)
        self.assertEqual([], adapter.sent)

    def test_timestamp_candidate_and_ambiguity_reach_model_without_confirmation(self):
        message = make_message(replied={"msgId": "unknown", "msgType": "interactivecard", "createdAt": 1800000000100})
        adapter = self.run_message(message, remembered_webhook=("候选全文", 1800000000000, 1800000000200, {}))
        self.assertIn("候选全文", adapter.events[0].text)
        self.assertIn("不能当作已确认原文", adapter.events[0].text)
        self.assertIsNone(adapter.events[0].reply_to_text)
        self.assertEqual([], adapter.sent)
        store = adapter._card_reply_store
        store.remember_webhook_delivery("conversation-1", "重叠消息", 1800000000000, 1800000000200, {})
        adapter.events.clear()
        asyncio.run(self.on_message(adapter, message))
        self.assertIn("引用原文未取得", adapter.events[0].text)
        self.assertNotIn("候选全文", adapter.events[0].text)

    def test_ordinary_chat_and_explicit_command(self):
        for words in ["你好", "重新来", "no", "/help"]:
            message = make_message()
            message.text.content = words
            adapter = self.run_message(message)
            self.assertEqual(words, adapter.events[0].text)
            self.assertEqual(words.startswith("/"), adapter.events[0].allow_gateway_control)

    def test_file_reply_keeps_media(self):
        adapter = self.run_message(make_message(replied={"msgId": "file", "msgType": "file", "content": {"fileName": "x.txt"}}))
        self.assertEqual(["resolved-file"], adapter.events[0].media_urls)
        self.assertEqual(["text/plain"], adapter.events[0].media_types)

    def test_inbound_original_survives_reopen_and_chat_boundary(self):
        message = make_message()
        message.text.content = "用户保存的全文" * 800
        adapter = self.run_message(message)
        store = self.reply_context.CardReplyStore(adapter._card_reply_store._state_dir)
        quoted = make_message(replied={"msgId": "incoming-1", "msgType": "text"})
        self.assertEqual(message.text.content, store.resolve_message("conversation-1", quoted))
        self.assertIsNone(store.resolve_message("another-chat", quoted))

    def test_oversized_material_is_readable_in_full_and_never_canned_reply(self):
        original = "超长原文" * 12000 + "FILE-TAIL-853"
        adapter = self.run_message(make_message(replied={"msgId": "big", "msgType": "text"}), remembered=("big", "track", original))
        event = adapter.events[0]
        files = list(Path(adapter._card_reply_store._state_dir).glob("quote-*.txt"))
        self.assertEqual(1, len(files))
        self.assertIn(original, files[0].read_text())
        self.assertIn(str(files[0]), event.text)
        self.assertTrue(event.text.startswith("这个怎么处理"))
        self.assertEqual([], adapter.sent)

    def test_historical_context_survives_new_session_without_other_chats(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict("os.environ", {"HERMES_HOME": home}):
            with closing(sqlite3.connect(str(Path(home) / "state.db"))) as db, db:
                db.executescript("CREATE TABLE sessions(id TEXT, source TEXT, chat_id TEXT); CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, platform_message_id TEXT);")
                db.executemany("INSERT INTO sessions VALUES (?, 'dingtalk', ?)", [("old", "conversation-1"), ("new", "conversation-1"), ("other", "another-chat")])
                db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", [
                    (1, "old", "user", "当时要求用绿色", 1800000000, "old-user"),
                    (2, "old", "assistant", "已改成绿色纸鹤", 1800000001, None),
                    (3, "new", "user", "后来的无关请求", 1800000010, None),
                    (4, "other", "user", "其他聊天隐私", 1800000000, None)])
            adapter = self.run_message(make_message(replied={"msgId": "old-bot", "msgType": "text", "createdAt": 1800000001100}), remembered_webhook=("已改成绿色纸鹤", 1800000001050, 1800000001150, {"msgId": "old-bot"}))
            text = adapter.events[0].text
            self.assertIn("当时要求用绿色", text)
            self.assertIn("已改成绿色纸鹤", text)
            self.assertNotIn("后来的无关请求", text)
            self.assertNotIn("其他聊天隐私", text)


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

    def test_legacy_database_keeps_first_originals_after_5000_other_deliveries(self):
        with tempfile.TemporaryDirectory() as state_dir:
            # Create the pre-fix tables directly, independent of the new initializer.
            with closing(sqlite3.connect(str(Path(state_dir) / "dingtalk_card_replies.db"))) as conn, conn:
                conn.executescript("""
                    CREATE TABLE card_replies (chat_id TEXT NOT NULL, carrier_id TEXT NOT NULL,
                        out_track_id TEXT NOT NULL, content TEXT NOT NULL, updated_epoch REAL NOT NULL,
                        PRIMARY KEY (chat_id, carrier_id));
                    CREATE TABLE webhook_replies (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL, message_id TEXT NOT NULL, request_started_ms INTEGER NOT NULL,
                        request_finished_ms INTEGER NOT NULL, content TEXT NOT NULL, updated_epoch REAL NOT NULL);
                """)
                conn.executemany("INSERT INTO card_replies VALUES (?, ?, ?, ?, ?)",
                                 [("old-chat", f"card-{i}", f"track-{i}", f"原文-{i}", i) for i in range(5000)])
                conn.executemany("INSERT INTO webhook_replies VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                                 [("old-chat", f"webhook-{i}", 1800000000000 + i * 10000,
                                   1800000000200 + i * 10000, f"原文-{i}", i) for i in range(5000)])
            store = self.reply_context.CardReplyStore(state_dir)
            self.assertTrue(store.remember_delivery("new-chat", "track-new", "最新", self.delivery_response(
                SimpleNamespace(carrier_id="new-card", success=True))))
            self.assertTrue(store.remember_webhook_delivery("new-chat", "最新", 1900000000000,
                                                            1900000000200, {"msgId": "new-webhook"}))
            reopened = self.reply_context.CardReplyStore(state_dir)
            self.assertEqual("原文-0", reopened.resolve("old-chat", "card-0"))
            first = make_message(replied={"msgId": "webhook-0", "msgType": "interactiveCard"})
            self.assertEqual("原文-0", reopened.resolve_message("old-chat", first))
            candidate = make_message(replied={"msgId": "unknown", "msgType": "interactiveCard",
                                              "createdAt": 1800000000100})
            self.assertIn("原文-0", reopened.prepare_reply("old-chat", candidate, self.reply_context.build_reply_kwargs(candidate)))
            with closing(reopened._connect()) as conn:
                self.assertEqual(5001, conn.execute("SELECT COUNT(*) FROM card_replies").fetchone()[0])
                self.assertEqual(5001, conn.execute("SELECT COUNT(*) FROM webhook_replies").fetchone()[0])

    def test_long_card_content_is_complete_on_create_and_edit(self):
        with tempfile.TemporaryDirectory() as state_dir:
            store = self.reply_context.CardReplyStore(state_dir)
            original = "卡片原文" * 6000 + "CREATE-TAIL"
            self.assertTrue(store.remember_delivery("chat", "track", original, self.delivery_response(
                SimpleNamespace(carrier_id="card", success=True))))
            self.assertEqual(original, store.resolve("chat", "card"))
            updated = "卡片新版" * 6000 + "EDIT-TAIL"
            self.assertTrue(store.update_content("chat", "track", updated))
            self.assertEqual(updated, self.reply_context.CardReplyStore(state_dir).resolve("chat", "card"))

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
