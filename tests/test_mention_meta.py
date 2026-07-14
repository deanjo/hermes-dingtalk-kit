"""Unit tests for the @-mention meta line (mentions.mention_meta_line).

DingTalk strips ``@nick`` tokens from ``text.content`` server-side and only
delivers the at list structurally (``atUsers`` with dingtalkId/staffId, no
nickname).  ``mention_meta_line`` surfaces the non-bot entries so the model
can resolve who "你/你们" refers to.  The module loads standalone (stdlib
only), so no Hermes runtime is needed.
"""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/mentions.py"
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"

BOT_ID = "$:LWCP_v1:$bot-self-id"


def _load_module():
    spec = importlib.util.spec_from_file_location("dingtalk_mentions_uut", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _at_user(dingtalk_id="", staff_id=""):
    return SimpleNamespace(dingtalk_id=dingtalk_id or None, staff_id=staff_id or None)


def _message(at_users=None, chatbot_user_id=BOT_ID, is_in_at_list=True):
    return SimpleNamespace(
        at_users=at_users or [],
        chatbot_user_id=chatbot_user_id,
        is_in_at_list=is_in_at_list,
    )


class MentionMetaLineTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_no_at_users_returns_empty(self):
        self.assertEqual("", self.mod.mention_meta_line(_message(), {}))

    def test_only_bot_mentioned_returns_empty(self):
        msg = _message(at_users=[_at_user(dingtalk_id=BOT_ID)])
        self.assertEqual("", self.mod.mention_meta_line(msg, {}))

    def test_bot_plus_org_member_uses_staff_id_tail(self):
        msg = _message(
            at_users=[
                _at_user(dingtalk_id=BOT_ID),
                _at_user(dingtalk_id="$:LWCP_v1:$other", staff_id="15528999368652879"),
            ]
        )
        line = self.mod.mention_meta_line(msg, {})
        self.assertIn("还@了 1 位群成员", line)
        self.assertIn("工号尾号2879", line)
        self.assertNotIn("15528999368652879", line)
        self.assertTrue(line.startswith("【消息元信息】"))

    def test_name_map_by_staff_id_wins_over_tail(self):
        msg = _message(
            at_users=[
                _at_user(dingtalk_id=BOT_ID),
                _at_user(dingtalk_id="$:LWCP_v1:$other", staff_id="1234567890"),
            ]
        )
        extra = {"at_user_names": {"1234567890": "邱耀莲"}}
        line = self.mod.mention_meta_line(msg, extra)
        self.assertIn("邱耀莲", line)
        self.assertNotIn("工号尾号", line)

    def test_name_map_by_dingtalk_id_for_non_org_user(self):
        msg = _message(
            at_users=[
                _at_user(dingtalk_id=BOT_ID),
                _at_user(dingtalk_id="$:LWCP_v1:$guest"),
            ]
        )
        extra = {"at_user_names": {"$:LWCP_v1:$guest": "外部顾问"}}
        line = self.mod.mention_meta_line(msg, extra)
        self.assertIn("外部顾问", line)

    def test_unknown_member_without_staff_id_or_map(self):
        msg = _message(
            at_users=[
                _at_user(dingtalk_id=BOT_ID),
                _at_user(dingtalk_id="$:LWCP_v1:$guest"),
            ]
        )
        line = self.mod.mention_meta_line(msg, {})
        self.assertIn("未知成员", line)

    def test_missing_bot_id_while_in_at_list_stays_silent(self):
        # Cannot tell which at_users entry is the bot -> never guess.
        msg = _message(
            at_users=[_at_user(dingtalk_id="$:LWCP_v1:$a", staff_id="111199")],
            chatbot_user_id=None,
            is_in_at_list=True,
        )
        self.assertEqual("", self.mod.mention_meta_line(msg, {}))

    def test_missing_bot_id_not_in_at_list_reports_all(self):
        # Free-response chat: bot not @'d, another human is.
        msg = _message(
            at_users=[_at_user(dingtalk_id="$:LWCP_v1:$a", staff_id="111199")],
            chatbot_user_id=None,
            is_in_at_list=False,
        )
        line = self.mod.mention_meta_line(msg, {})
        self.assertIn("还@了 1 位群成员", line)
        self.assertIn("工号尾号1199", line)

    def test_multiple_others_counted_and_joined(self):
        msg = _message(
            at_users=[
                _at_user(dingtalk_id=BOT_ID),
                _at_user(dingtalk_id="$:LWCP_v1:$a", staff_id="1234567890"),
                _at_user(dingtalk_id="$:LWCP_v1:$b", staff_id="9876540001"),
            ]
        )
        extra = {"at_user_names": {"1234567890": "邱耀莲"}}
        line = self.mod.mention_meta_line(msg, extra)
        self.assertIn("还@了 2 位群成员", line)
        self.assertIn("邱耀莲、工号尾号0001", line)

    def test_malformed_name_map_is_ignored(self):
        msg = _message(
            at_users=[
                _at_user(dingtalk_id=BOT_ID),
                _at_user(dingtalk_id="$:LWCP_v1:$a", staff_id="1234567890"),
            ]
        )
        line = self.mod.mention_meta_line(msg, {"at_user_names": "not-a-dict"})
        self.assertIn("工号尾号7890", line)


class AdapterWiringTest(unittest.TestCase):
    def test_adapter_injects_mention_meta_for_group_messages(self):
        text = ADAPTER_PATH.read_text(encoding="utf-8")
        self.assertIn("mention_meta_line(message", text)
        self.assertIn("if is_group:", text)
        # Both import paths must export the helper.
        self.assertEqual(2, text.count("mention_meta_line, should_process_message"))


if __name__ == "__main__":
    unittest.main()
