"""Unit tests for the admin-gated DingTalk private-message tool.

The module under test loads standalone: its framework symbols
(``get_session_env`` / ``tool_result`` / ``tool_error``) are imported lazily
inside functions with os-env / json fallbacks, so no Hermes runtime is needed.
Network calls are stubbed by monkeypatching module-level helpers.
"""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/private_send.py"

_SESSION_KEYS = (
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_USER_ID_ALT",
    "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_MESSAGE_ID",
)
_CFG_KEYS = (
    "DINGTALK_KIT_STATE_DIR",
    "DINGTALK_PRIVATE_MESSAGE_ADMINS",
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("dingtalk_private_send_uut", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrivateSendTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()
        self._tmp = tempfile.TemporaryDirectory(prefix="dt-private-test-")
        self._saved = {k: os.environ.get(k) for k in (*_CFG_KEYS, *_SESSION_KEYS)}
        os.environ["DINGTALK_KIT_STATE_DIR"] = self._tmp.name
        os.environ["DINGTALK_CLIENT_ID"] = "test-client"
        os.environ["DINGTALK_CLIENT_SECRET"] = "test-secret"
        os.environ["DINGTALK_PRIVATE_MESSAGE_ADMINS"] = "admin-staff-1"
        self._set_identity()
        # stub network so no real DingTalk call happens
        self.sent = []
        self.mod._send_private_markdown = lambda uid, title, text: (
            self.sent.append((uid, title, text)) or {"ok": True, "process_query_key": "pqk"}
        )
        self.mod._search_user_by_name = lambda name: {"ok": True, "user_ids": ["resolved-uid"]}

    def tearDown(self):
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self._tmp.cleanup()

    def _set_identity(self, *, user_id="enc-uid", user_id_alt="admin-staff-1",
                      name="Admin", session_id="sess1", message_id="m1"):
        os.environ["HERMES_SESSION_USER_ID"] = user_id
        os.environ["HERMES_SESSION_USER_ID_ALT"] = user_id_alt
        os.environ["HERMES_SESSION_USER_NAME"] = name
        os.environ["HERMES_SESSION_ID"] = session_id
        os.environ["HERMES_SESSION_MESSAGE_ID"] = message_id

    def _call(self, **args):
        return json.loads(self.mod.handle_dingtalk_send_private(args))

    # ------------------------------------------------------------------ #
    def test_non_admin_is_denied(self):
        os.environ["DINGTALK_PRIVATE_MESSAGE_ADMINS"] = "someone-else"
        out = self._call(action="prepare", user_id="u1", text="hi")
        self.assertIn("Not authorized", out.get("error", ""))
        self.assertEqual(self.sent, [])

    def test_empty_allowlist_denies_everyone(self):
        os.environ["DINGTALK_PRIVATE_MESSAGE_ADMINS"] = ""
        out = self._call(action="prepare", user_id="u1", text="hi")
        self.assertIn("Not authorized", out.get("error", ""))

    def test_admin_by_hash_is_allowed(self):
        os.environ["DINGTALK_PRIVATE_MESSAGE_ADMINS"] = hashlib.sha256(
            b"admin-staff-1"
        ).hexdigest()
        out = self._call(action="prepare", user_id="u1", text="hi")
        self.assertEqual(out.get("status"), "preview")

    def test_prepare_returns_preview_and_code(self):
        out = self._call(action="prepare", user_id="u1", title="T", text="body")
        self.assertEqual(out.get("status"), "preview")
        self.assertTrue(out.get("token"))
        self.assertRegex(str(out.get("confirm_code")), r"^\d{4}$")
        self.assertEqual(out.get("recipient_user_id"), "u1")

    def test_prepare_requires_text(self):
        out = self._call(action="prepare", user_id="u1", text="")
        self.assertIn("text", out.get("error", "").lower())

    def test_same_turn_confirm_is_rejected(self):
        prep = self._call(action="prepare", user_id="u1", text="body")  # message_id m1
        out = self._call(action="confirm", token=prep["token"],
                         confirm_code=prep["confirm_code"])  # still m1
        self.assertIn("same-turn", out.get("error", "").lower())
        self.assertEqual(self.sent, [])

    def test_cross_turn_confirm_sends(self):
        prep = self._call(action="prepare", user_id="u1", title="T", text="body")
        self._set_identity(message_id="m2")  # new human turn
        out = self._call(action="confirm", token=prep["token"], confirm_code=prep["confirm_code"])
        self.assertEqual(out.get("status"), "sent")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "u1")

    def test_bad_confirm_code_rejected(self):
        prep = self._call(action="prepare", user_id="u1", text="body")
        self._set_identity(message_id="m2")
        out = self._call(action="confirm", token=prep["token"], confirm_code="0000"
                         if prep["confirm_code"] != "0000" else "1111")
        self.assertIn("code", out.get("error", "").lower())
        self.assertEqual(self.sent, [])

    def test_confirm_by_other_sender_rejected(self):
        os.environ["DINGTALK_PRIVATE_MESSAGE_ADMINS"] = "admin-staff-1,admin-staff-2"
        prep = self._call(action="prepare", user_id="u1", text="body")  # by admin-staff-1
        self._set_identity(user_id_alt="admin-staff-2", message_id="m2")  # different admin
        out = self._call(action="confirm", token=prep["token"], confirm_code=prep["confirm_code"])
        self.assertIn("prepared", out.get("error", "").lower())
        self.assertEqual(self.sent, [])

    def test_recipient_ambiguous(self):
        self.mod._search_user_by_name = lambda name: {"ok": True, "user_ids": ["a", "b"]}
        out = self._call(action="prepare", recipient_name="Zhang", text="body")
        self.assertIn("ambiguous", out.get("error", "").lower())

    def test_recipient_not_found(self):
        self.mod._search_user_by_name = lambda name: {"ok": True, "user_ids": []}
        out = self._call(action="prepare", recipient_name="Nobody", text="body")
        self.assertIn("not_found", out.get("error", "").lower())

    def test_cancel_removes_pending(self):
        prep = self._call(action="prepare", user_id="u1", text="body")
        cancelled = self._call(action="cancel", token=prep["token"])
        self.assertEqual(cancelled.get("status"), "cancelled")
        self._set_identity(message_id="m2")
        out = self._call(action="confirm", token=prep["token"], confirm_code=prep["confirm_code"])
        self.assertIn("token", out.get("error", "").lower())
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
