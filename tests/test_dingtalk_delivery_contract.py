"""Execute DingTalkAdapter.send's mention and errcode delivery contract."""

from __future__ import annotations

import ast
import asyncio
import copy
import time
import unittest
from unittest import mock
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"


@dataclass
class SendResult:
    success: bool
    message_id: str = ""
    error: str = ""
    raw_response: object = None


class FakeLogger:
    def debug(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None


def load_send_function():
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
    send_node = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter":
            send_node = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "send"
                ),
                None,
            )
            break
    if send_node is None:
        raise RuntimeError("DingTalkAdapter.send not found")
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            copy.deepcopy(send_node),
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    mark_calls = []
    namespace = {
        "SendResult": SendResult,
        "logger": FakeLogger(),
        # H1 治理第 5 项：send 入口先过出站闸门（overlays/.../delivery_gate.py）。
        # 本沙箱只测投递契约，注入放行版即可；闸门自身的判定由
        # tests/test_dingtalk_outbound_gate.py 覆盖。
        "blocked_send_result": lambda *args, **kwargs: None,
        "httpx": SimpleNamespace(TimeoutException=TimeoutError),
        "time": time,
        "uuid": SimpleNamespace(
            uuid4=lambda: SimpleNamespace(hex="1234567890abcdef")
        ),
        # V2 delivery flag point (I1: webhook fallback also flags delivery).
        "mark_h1_turn_delivered": lambda *args, **kwargs: mark_calls.append(
            (args, kwargs)
        ),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    module_send = namespace["send"]
    module_send._mark_calls = mark_calls
    return module_send


class FakeModels:
    def __getattr__(self, name):
        return lambda **kwargs: SimpleNamespace(**kwargs)


def load_card_methods():
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
    adapter_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter"
    )
    methods = {
        node.name: copy.deepcopy(node)
        for node in adapter_class.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"_create_and_stream_card", "edit_message", "_stream_card_content"}
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            methods["_create_and_stream_card"],
            methods["edit_message"],
            methods["_stream_card_content"],
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "SendResult": SendResult,
        "dingtalk_card_models": FakeModels(),
        "logger": FakeLogger(),
        "mark_h1_turn_delivered": lambda *args, **kwargs: None,
        "tea_util_models": FakeModels(),
        "traceback": SimpleNamespace(format_exc=lambda: "traceback"),
        "uuid": SimpleNamespace(
            uuid4=lambda: SimpleNamespace(hex="abcdef1234567890")
        ),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["_create_and_stream_card"], namespace["edit_message"], namespace["_stream_card_content"]


class FakeResponse:
    def __init__(self, *, status_code=200, body=None, text=""):
        self.status_code = status_code
        self.body = {"errcode": 0} if body is None else body
        self.text = text

    def json(self):
        return self.body


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, *, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeAdapter:
    MAX_MESSAGE_LENGTH = 20_000

    def __init__(self, response):
        self.name = "dingtalk"
        self._card_template_id = "card-template"
        self._card_sdk = object()
        self._message_contexts = {"conv-1": object()}
        self._http_client = FakeHttpClient(response)
        self._streaming_cards = {}
        self._card_reply_store = FakeCardReplyStore()
        self.card_calls = 0
        self.done_reactions = []

    def _get_valid_webhook(self, chat_id):
        return "https://example.invalid/session-webhook", 1

    @staticmethod
    def _normalize_markdown(content):
        return "normalized:" + content

    async def _close_streaming_siblings(self, chat_id):
        self.card_calls += 1

    async def _create_and_stream_card(self, *args, **kwargs):
        self.card_calls += 1
        return SendResult(success=True, message_id="card-1")

    def _fire_done_reaction(self, chat_id):
        self.done_reactions.append(chat_id)


class FakeCardReplyStore:
    def __init__(self):
        self.remembered = []
        self.webhook_remembered = []
        self.updated = []
        self.invalidated = []

    def invalidate_content(self, *args):
        self.invalidated.append(args)
        return True

    def remember_delivery(self, *args):
        self.remembered.append(args)
        return True

    def update_content(self, *args):
        self.updated.append(args)
        return True

    def remember_webhook_delivery(self, *args):
        self.webhook_remembered.append(args)
        return True


class FakeCardSdk:
    def __init__(self, response):
        self.response = response

    async def create_card_with_options_async(self, *args):
        return SimpleNamespace()

    async def deliver_card_with_options_async(self, *args):
        return self.response


class FakeCardAdapter:
    MAX_MESSAGE_LENGTH = 20_000
    name = "dingtalk"

    def __init__(self, response):
        self._card_template_id = "card-template"
        self._card_sdk = FakeCardSdk(response)
        self._card_reply_store = FakeCardReplyStore()
        self._robot_code = "robot-code"
        self._streaming_cards = {}
        self.done_reactions = []
        self.streamed = []

    async def _get_access_token(self):
        return "access-token"

    async def _stream_card_content(self, message_id, token, content, finalize=False):
        self.streamed.append((message_id, token, content, finalize))

    def _fire_done_reaction(self, chat_id):
        self.done_reactions.append(chat_id)


class DingTalkDeliveryContractTest(unittest.TestCase):
    def setUp(self):
        self.send = load_send_function()
        self.create_card, self.edit_card, self.stream_card = load_card_methods()

    def run_send(self, adapter, **kwargs):
        return asyncio.run(self.send(adapter, "conv-1", "hello", **kwargs))

    def test_at_user_ids_force_webhook_and_build_structured_payload(self):
        adapter = FakeAdapter(FakeResponse(body={"errcode": 0, "errmsg": "ok"}))

        result = self.run_send(
            adapter,
            metadata={"at_user_ids": ["staff-1", 2002]},
        )

        self.assertTrue(result.success, result)
        self.assertEqual(0, adapter.card_calls)
        self.assertEqual(1, len(adapter._http_client.calls))
        self.assertEqual(
            {
                "msgtype": "markdown",
                "markdown": {"title": "Hermes", "text": "normalized:hello"},
                "at": {"atUserIds": ["staff-1", "2002"], "isAtAll": False},
            },
            adapter._http_client.calls[0]["json"],
        )

    def test_webhook_sends_and_saves_over_20000_characters_without_truncation(self):
        adapter = FakeAdapter(FakeResponse())
        original = "完整正文" * 6000 + "TAIL-24000"
        result = asyncio.run(self.send(adapter, "conv-1", original, metadata={"at_user_ids": ["staff"]}))
        self.assertTrue(result.success)
        self.assertEqual("normalized:" + original, adapter._http_client.calls[0]["json"]["markdown"]["text"])
        self.assertEqual("normalized:" + original, adapter._card_reply_store.webhook_remembered[0][1])

    def test_card_sdk_receives_full_content_above_20000_characters(self):
        adapter = FakeCardAdapter(SimpleNamespace())
        adapter._card_sdk.streaming_update_with_options_async = mock.AsyncMock()
        original = "完整卡片" * 6000 + "SDK-TAIL"
        asyncio.run(self.stream_card(adapter, "track", "fake-token", original, finalize=True))
        request = adapter._card_sdk.streaming_update_with_options_async.call_args.args[0]
        self.assertEqual(original, request.content)

    def test_single_string_at_user_id_is_normalized_to_a_list(self):
        adapter = FakeAdapter(FakeResponse())

        result = self.run_send(adapter, metadata={"at_user_ids": "staff-1"})

        self.assertTrue(result.success, result)
        payload = adapter._http_client.calls[0]["json"]
        self.assertEqual(["staff-1"], payload["at"]["atUserIds"])

    def test_http_200_with_nonzero_errcode_is_a_delivery_failure(self):
        adapter = FakeAdapter(
            FakeResponse(body={"errcode": 310000, "errmsg": "robot removed"})
        )

        result = self.run_send(adapter, metadata={"at_user_ids": ["staff-1"]})

        self.assertFalse(result.success, result)
        self.assertIn("DingTalk errcode 310000", result.error)
        self.assertIn("robot removed", result.error)
        self.assertEqual("rejected", result.raw_response["delivery_outcome"])
        self.assertEqual([], adapter._card_reply_store.webhook_remembered)

    def test_zero_errcode_returns_success_and_final_reply_reaction(self):
        adapter = FakeAdapter(FakeResponse(body={"errcode": 0}))

        result = self.run_send(
            adapter,
            reply_to="inbound-1",
            metadata={"at_user_ids": ["staff-1"]},
        )

        self.assertTrue(result.success, result)
        self.assertEqual("1234567890ab", result.message_id)
        self.assertEqual("delivered", result.raw_response["delivery_outcome"])
        self.assertEqual(["conv-1"], adapter.done_reactions)
        self.assertEqual(1, len(adapter._card_reply_store.webhook_remembered))
        remembered = adapter._card_reply_store.webhook_remembered[0]
        self.assertEqual("conv-1", remembered[0])
        self.assertEqual("normalized:hello", remembered[1])
        self.assertLessEqual(remembered[2], remembered[3])
        self.assertEqual({"errcode": 0}, remembered[4])

    def test_connection_reset_is_machine_readable_unknown_outcome(self):
        adapter = FakeAdapter(ConnectionResetError("Connection reset by peer"))

        result = self.run_send(
            adapter,
            metadata={"at_user_ids": ["staff-1"]},
        )

        self.assertFalse(result.success, result)
        self.assertEqual("Connection reset by peer", result.error)
        self.assertEqual("unknown", result.raw_response["delivery_outcome"])
        self.assertEqual(1, len(adapter._http_client.calls))

    def test_successful_send_with_failed_storage_reports_degraded_recovery(self):
        adapter = FakeAdapter(FakeResponse())
        adapter._card_reply_store.remember_webhook_delivery = lambda *args: False
        result = self.run_send(adapter, metadata={"at_user_ids": ["staff-1"]})
        self.assertTrue(result.success)
        self.assertEqual("delivered", result.raw_response["delivery_outcome"])
        self.assertIs(result.raw_response["reply_context_saved"], False)
        self.assertEqual(1, len(adapter._http_client.calls))

    def test_legacy_recovery_prompt_is_saved_like_every_delivered_reply(self):
        adapter = FakeAdapter(FakeResponse())
        result = self.run_send(adapter, metadata={"reply_recovery_prompt": True, "delivery_class": "business_error"})
        self.assertTrue(result.success)
        self.assertEqual(0, adapter.card_calls)
        self.assertEqual(1, len(adapter._card_reply_store.webhook_remembered))
        self.assertTrue(result.raw_response["reply_context_saved"])

    def test_card_delivery_response_is_saved_for_future_quote_resolution(self):
        response = SimpleNamespace(
            body=SimpleNamespace(
                result=[SimpleNamespace(carrier_id="carrier-27", success=True)]
            )
        )
        adapter = FakeCardAdapter(response)
        message = SimpleNamespace(
            conversation_id="conv-1",
            conversation_type="2",
            sender_staff_id="staff-1",
        )

        result = asyncio.run(
            self.create_card(adapter, "conv-1", message, "初始卡片内容", finalize=True)
        )

        self.assertTrue(result.success, result)
        self.assertEqual("hermes_abcdef123456", result.message_id)
        self.assertEqual(
            [("conv-1", "hermes_abcdef123456", "初始卡片内容", response)],
            adapter._card_reply_store.remembered,
        )

    def test_card_edit_updates_saved_text_by_out_track_id(self):
        adapter = FakeCardAdapter(SimpleNamespace())

        result = asyncio.run(
            self.edit_card(
                adapter,
                "conv-1",
                "hermes_abcdef123456",
                "最终完整内容",
                finalize=False,
            )
        )

        self.assertTrue(result.success, result)
        self.assertEqual(
            [("conv-1", "hermes_abcdef123456", "最终完整内容")],
            adapter._card_reply_store.updated,
        )
        self.assertEqual([("conv-1", "hermes_abcdef123456")], adapter._card_reply_store.invalidated)

    def test_failed_invalidation_prevents_remote_card_edit(self):
        adapter = FakeCardAdapter(SimpleNamespace())
        adapter._card_reply_store.invalidate_content = lambda *args: False
        result = asyncio.run(self.edit_card(adapter, "conv-1", "track", "新正文"))
        self.assertFalse(result.success)
        self.assertEqual([], adapter.streamed)
        self.assertEqual([], adapter._card_reply_store.updated)

    def test_failed_remote_card_edit_does_not_save_unseen_content(self):
        adapter = FakeCardAdapter(SimpleNamespace())
        adapter._stream_card_content = mock.AsyncMock(side_effect=ConnectionError("unknown result"))
        result = asyncio.run(self.edit_card(adapter, "conv-1", "track", "新正文"))
        self.assertFalse(result.success)
        self.assertEqual([("conv-1", "track")], adapter._card_reply_store.invalidated)
        self.assertEqual([], adapter._card_reply_store.updated)


if __name__ == "__main__":
    unittest.main()
