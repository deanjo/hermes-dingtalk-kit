import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SESSION_PY = ROOT / "overlays/hermes/gateway/session.py"


def load_actual_session_key_checker():
    text = SESSION_PY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_is_session_key_unsafe":
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {}
            exec(compile(module, str(SESSION_PY), "exec"), namespace)
            return namespace["_is_session_key_unsafe"]
    raise AssertionError("_is_session_key_unsafe not found in overlay session.py")


class SessionKeySlashTest(unittest.TestCase):
    def setUp(self):
        self.unsafe_session_key = load_actual_session_key_checker()

    def test_dingtalk_base64_keys_are_allowed(self):
        allowed = [
            "agent:main:dingtalk:dm:cidEXAMPLEbase64/WithSlashAAAA=",
            "agent:main:dingtalk:group:cidEXAMPLEbase64/WithSlash==:10000000000000001",
            "agent:main:dingtalk:dm:$:LWCP_v1:$EXAMPLE/EncryptedIdAA==",
        ]
        for key in allowed:
            self.assertFalse(self.unsafe_session_key(key), key)

    def test_path_shaped_values_are_blocked(self):
        blocked = [
            "../../etc/passwd",
            "agent:..:x",
            "/etc/passwd",
            "~/x",
            "C:/windows/system32",
            "C:\\windows",
            "a\\b",
        ]
        for key in blocked:
            self.assertTrue(self.unsafe_session_key(key), key)


if __name__ == "__main__":
    unittest.main()
