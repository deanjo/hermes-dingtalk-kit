"""Execute DingTalkAdapter.send's mention and errcode delivery contract."""

from __future__ import annotations

import ast
import asyncio
import copy
import unittest
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
    namespace = {
        "SendResult": SendResult,
        "logger": FakeLogger(),
        "httpx": SimpleNamespace(TimeoutException=TimeoutError),
        "uuid": SimpleNamespace(
            uuid4=lambda: SimpleNamespace(hex="1234567890abcdef")
        ),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["send"]


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


class DingTalkDeliveryContractTest(unittest.TestCase):
    def setUp(self):
        self.send = load_send_function()

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


if __name__ == "__main__":
    unittest.main()
