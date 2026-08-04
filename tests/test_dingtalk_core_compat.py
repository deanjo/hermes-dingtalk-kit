"""Compatibility checks for the official Hermes DingTalk adapter surface."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "overlays/hermes/plugins/platforms/dingtalk"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MessageType:
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class DingTalkCoreCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = load_module("dingtalk_text_core_compat", PLUGIN_ROOT / "text.py")
        reply_context = load_module("reply_context", PLUGIN_ROOT / "reply_context.py")
        del reply_context
        cls.media = load_module("dingtalk_media_core_compat", PLUGIN_ROOT / "media.py")

    def test_card_and_interactive_card_text_are_preserved(self):
        card = SimpleNamespace(
            text=None,
            rich_text=None,
            rich_text_content=None,
            message_type="card",
            extensions={
                "card": {
                    "title": "Q3经营分析报告",
                    "content": {"url": "https://dingtalk.com/doc/abc123"},
                }
            },
        )
        interactive = SimpleNamespace(
            text=None,
            rich_text=None,
            rich_text_content=None,
            message_type="interactiveCard",
            extensions={
                "content": {
                    "title": "项目看板",
                    "biz_custom_action_url": "https://dingtalk.com/doc/kanban",
                }
            },
        )

        self.assertEqual(
            "[文档] Q3经营分析报告 https://dingtalk.com/doc/abc123",
            self.text.extract_text(card),
        )
        self.assertEqual(
            "[文档卡片] 项目看板 https://dingtalk.com/doc/kanban",
            self.text.extract_text(interactive),
        )

    def test_rich_text_voice_is_not_reset_to_text(self):
        message = SimpleNamespace(
            image_content=None,
            rich_text_content=None,
            rich_text=[{"type": "voice", "downloadCode": "voice-1"}],
            message_type="richText",
            extensions={},
        )

        media_type, urls, mime_types = self.media.extract_media(message, MessageType)

        self.assertEqual(MessageType.VOICE, media_type)
        self.assertEqual(["voice-1"], urls)
        self.assertEqual(["audio"], mime_types)

    def test_image_extension_payload_is_a_photo(self):
        message = SimpleNamespace(
            image_content=None,
            rich_text_content=None,
            rich_text=None,
            message_type="image",
            extensions={"content": {"downloadCode": "image-1"}},
        )

        media_type, urls, mime_types = self.media.extract_media(message, MessageType)

        self.assertEqual(MessageType.PHOTO, media_type)
        self.assertEqual(["image-1"], urls)
        self.assertEqual(["application/octet-stream"], mime_types)

    def test_adapter_keeps_official_compatibility_methods(self):
        tree = ast.parse((PLUGIN_ROOT / "adapter.py").read_text(encoding="utf-8"))
        adapter = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter"
        )
        methods = {
            node.name
            for node in adapter.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertTrue(
            {
                "_extract_media",
                "_extract_text",
                "_is_user_allowed",
                "_message_matches_mention_patterns",
                "_should_process_message",
            }.issubset(methods)
        )
        self.assertIn("_IncomingHandler", (PLUGIN_ROOT / "adapter.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
