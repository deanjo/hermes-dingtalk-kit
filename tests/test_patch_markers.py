from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DINGTALK_PLUGIN_ROOT = ROOT / "overlays/hermes/plugins/platforms/dingtalk"


def read_dingtalk_plugin_text():
    return "\n".join(
        path.read_text()
        for path in sorted(DINGTALK_PLUGIN_ROOT.glob("**/*.py"))
    )


class PatchMarkersTest(unittest.TestCase):
    def test_dingtalk_adapter_has_raw_process_and_reply_context(self):
        text = read_dingtalk_plugin_text()
        self.assertIn("async def raw_process", text)
        self.assertIn("_REPLY_ORIGINAL_UNAVAILABLE", text)
        self.assertIn("reply_to_message_id", text)
        self.assertIn("reply_to_text", text)

    def test_dingtalk_adapter_wires_incoming_handler_and_markdown_wrapper(self):
        text = (DINGTALK_PLUGIN_ROOT / "adapter.py").read_text()
        self.assertIn("handler_cls = make_incoming_handler", text)
        self.assertIn("register_callback_handler", text)
        self.assertIn("def _normalize_markdown", text)

    def test_gateway_run_has_reply_v2_and_session_id(self):
        text = (ROOT / "overlays/hermes/gateway/run.py").read_text()
        self.assertIn("_REPLY_ORIGINAL_UNAVAILABLE", text)
        self.assertIn("if not _has_reply_assistant_history", text)
        self.assertIn("reply_to=source.message_id", text)
        self.assertIn("只有当引用目标唯一明确时才能继续处理", text)
        self.assertIn("session_id=context.session_id", text)

    def test_session_has_dingtalk_slash_safe_key_validator(self):
        text = (ROOT / "overlays/hermes/gateway/session.py").read_text()
        self.assertIn("def _is_session_key_unsafe", text)
        self.assertIn('("session_key", session_key, _is_session_key_unsafe)', text)
        self.assertIn('("session_id", session_id, _is_path_unsafe)', text)


if __name__ == "__main__":
    unittest.main()
