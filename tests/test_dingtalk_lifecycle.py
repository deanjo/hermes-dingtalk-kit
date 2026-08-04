"""Lifecycle contract for the bundled DingTalk adapter."""

from __future__ import annotations

import ast
import asyncio
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"


def load_disconnect():
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"), filename=str(ADAPTER_PATH))
    adapter_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter"
    )
    method = next(
        node
        for node in adapter_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "disconnect"
    )
    module = ast.Module(body=[copy.deepcopy(method)], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "asyncio": asyncio,
        "logger": SimpleNamespace(debug=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None),
    }
    exec(compile(module, str(ADAPTER_PATH), "exec"), namespace)
    return namespace["disconnect"]


class FakeDeduplicator:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


class PartialAdapter:
    """State available immediately after __init__, before connect succeeds."""

    name = "dingtalk"

    def __init__(self):
        self._running = False
        self._stream_client = None
        self._stream_task = None
        self._bg_tasks = set()
        self._streaming_cards = {}
        self._http_client = None
        self._session_webhooks = {}
        self._message_contexts = {}
        self._done_emoji_fired = set()
        self._dedup = FakeDeduplicator()
        self.disconnected = False

    def _mark_disconnected(self):
        self.disconnected = True

    async def _close_streaming_siblings(self, chat_id):
        raise AssertionError(f"unexpected streaming card: {chat_id}")


class DingTalkLifecycleTest(unittest.TestCase):
    def test_disconnect_tolerates_failed_connect_partial_state(self):
        adapter = PartialAdapter()

        asyncio.run(load_disconnect()(adapter))

        self.assertTrue(adapter.disconnected)
        self.assertTrue(adapter._dedup.cleared)


if __name__ == "__main__":
    unittest.main()
