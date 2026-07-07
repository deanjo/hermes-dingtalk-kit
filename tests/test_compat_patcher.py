from pathlib import Path
import importlib.util
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".baseline/hermes"
SCRIPT = ROOT / "scripts/compat_patcher.py"


def load_patcher():
    spec = importlib.util.spec_from_file_location("hermes_dingtalk_compat_patcher", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CompatPatcherTest(unittest.TestCase):
    def setUp(self):
        self.patcher = load_patcher()

    def make_target(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "hermes"
        shutil.copytree(BASELINE / "gateway", root / "gateway")
        return root

    def test_apply_is_idempotent_and_verifiable(self):
        root = self.make_target()

        check_report = self.patcher.build_report(root, "check")
        self.assertTrue(check_report["ok"], check_report)
        self.assertEqual(3, check_report["changed_count"], check_report)

        apply_report = self.patcher.build_report(root, "apply")
        self.assertTrue(apply_report["ok"], apply_report)
        self.assertEqual(
            ["gateway/run.py", "gateway/session.py", "gateway/session_context.py"],
            apply_report["changed_files"],
        )

        before_second = {
            path: (root / path).read_text(encoding="utf-8")
            for path in apply_report["changed_files"]
        }
        second_report = self.patcher.build_report(root, "apply")
        self.assertTrue(second_report["ok"], second_report)
        self.assertEqual(0, second_report["changed_count"], second_report)
        after_second = {
            path: (root / path).read_text(encoding="utf-8")
            for path in apply_report["changed_files"]
        }
        self.assertEqual(before_second, after_second)

        verify_report = self.patcher.build_report(root, "verify")
        self.assertTrue(verify_report["ok"], verify_report)

    def test_session_context_bridge_sets_and_clears_new_vars(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        module = load_module(
            root / "gateway/session_context.py",
            f"patched_session_context_{id(root)}",
        )
        tokens = module.set_session_vars(
            chat_type="group",
            user_id_alt="staff-42",
            session_id="sid-42",
        )
        self.assertEqual("group", module.get_session_env("HERMES_SESSION_CHAT_TYPE"))
        self.assertEqual("staff-42", module.get_session_env("HERMES_SESSION_USER_ID_ALT"))
        self.assertEqual("sid-42", module.get_session_env("HERMES_SESSION_ID"))

        module.clear_session_vars(tokens)
        self.assertEqual("", module.get_session_env("HERMES_SESSION_CHAT_TYPE"))
        self.assertEqual("", module.get_session_env("HERMES_SESSION_USER_ID_ALT"))
        self.assertEqual("", module.get_session_env("HERMES_SESSION_ID"))

    def test_missing_anchor_stops_without_writing(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        original = run_py.read_text(encoding="utf-8")
        run_py.write_text(
            original.replace("            reply_snippet = event.reply_to_text[:500]\n", ""),
            encoding="utf-8",
        )
        damaged = run_py.read_text(encoding="utf-8")

        report = self.patcher.build_report(root, "check")
        self.assertFalse(report["ok"], report)
        self.assertEqual(damaged, run_py.read_text(encoding="utf-8"))
        self.assertIn(
            "run.reply_context_sentinel_branch",
            {
                item["name"]
                for item in report["results"]
                if item["status"] == "anchor-missing"
            },
        )

    def test_apply_session_env_fields_preserves_profile_keyword(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        run_py.write_text(
            text.replace(
                "            message_id=str(context.source.message_id) if context.source.message_id else \"\",\n"
                "            async_delivery=_async_delivery,\n",
                "            message_id=str(context.source.message_id) if context.source.message_id else \"\",\n"
                "            profile=getattr(context.source, \"profile\", \"\") or \"\",\n"
                "            async_delivery=_async_delivery,\n",
            ),
            encoding="utf-8",
        )

        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        patched = run_py.read_text(encoding="utf-8")
        self.assertIn("            session_id=context.session_id,\n", patched)
        self.assertIn(
            "            profile=getattr(context.source, \"profile\", \"\") or \"\",\n",
            patched,
        )
        verify_report = self.patcher.build_report(root, "verify")
        self.assertTrue(verify_report["ok"], verify_report)

    def test_write_failure_rolls_back_prior_files(self):
        root = self.make_target()
        before = {
            path: (root / path).read_text(encoding="utf-8")
            for path in (
                "gateway/run.py",
                "gateway/session.py",
                "gateway/session_context.py",
            )
        }

        real_atomic_write = self.patcher._atomic_write

        def fail_on_session(path, text):
            if path.name == "session.py":
                raise OSError("simulated write failure")
            return real_atomic_write(path, text)

        with mock.patch.object(self.patcher, "_atomic_write", side_effect=fail_on_session):
            report = self.patcher.build_report(root, "apply")

        self.assertFalse(report["ok"], report)
        self.assertEqual(0, report["changed_count"], report)
        self.assertIn(
            "apply.write_files",
            {
                item["name"]
                for item in report["results"]
                if item["status"] == "write-failed"
            },
        )
        after = {
            path: (root / path).read_text(encoding="utf-8")
            for path in before
        }
        self.assertEqual(before, after)

    def test_verify_rejects_marker_that_only_appears_in_comment(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        run_py.write_text(
            text.replace(
                "            session_id=context.session_id,\n",
                "            # session_id=context.session_id,\n",
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "run.session_env_fields",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_session_checker_tuple_only_in_comment(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                '            ("session_key", session_key, _is_session_key_unsafe),\n',
                '            # ("session_key", session_key, _is_session_key_unsafe),\n',
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.path_sensitive_validation",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_commented_session_key_checker_body(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        end = text.index("\n\n@dataclass", start)
        fake_checker = (
            "def _is_session_key_unsafe(value: object) -> bool:\n"
            "    # if \"..\" in s or \"\\\\\" in s:\n"
            "    # if s.startswith((\"/\", \"~\")):\n"
            "    # return len(s) >= 2 and s[0].isalpha() and s[1] == \":\" and s[2:3] in (\"/\", \"\\\\\")\n"
            "    return False\n"
        )
        session_py.write_text(text[:start] + fake_checker + text[end:], encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_missing_session_key_string_coercion(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        session_py.write_text(
            text[:start] + text[start:].replace("    s = str(value)\n", "", 1),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_late_session_key_string_coercion(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:]
        body = body.replace("    s = str(value)\n", "", 1)
        body = body.replace(
            '    return len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2:3] in ("/", "\\\\")\n',
            '    return len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2:3] in ("/", "\\\\")\n'
            "    s = str(value)\n",
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_unreachable_session_key_guards(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace(
            "    s = str(value)\n",
            "    s = str(value)\n"
            "    return False\n",
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_unreachable_true_inside_session_key_guard(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace(
            '    if ".." in s or "\\\\" in s:\n'
            "        return True\n",
            '    if ".." in s or "\\\\" in s:\n'
            "        return False\n"
            "        return True\n",
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_extra_name_in_startswith_tuple(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace(
            '    if s.startswith(("/", "~")):\n',
            '    if s.startswith(("/", "~", missing_prefix)):\n',
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_extra_name_in_drive_separator_tuple(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace(
            's[2:3] in ("/", "\\\\")',
            's[2:3] in ("/", "\\\\", missing_separator)',
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_extra_startswith_argument(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace(
            '    if s.startswith(("/", "~")):\n',
            '    if s.startswith(("/", "~"), 1):\n',
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_extra_isalpha_argument(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace("s[0].isalpha()", "s[0].isalpha(missing_arg)", 1)
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_len_keyword_argument(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace("len(s)", "len(s, missing=1)", 1)
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_drive_slice_step(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace("s[2:3]", "s[2:3:0]", 1)
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_unordered_session_key_return_operands(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        start = text.index("def _is_session_key_unsafe")
        body = text[start:].replace(
            '    return len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2:3] in ("/", "\\\\")\n',
            '    return s[0].isalpha() and s[1] == ":" and len(s) >= 2 and s[2:3] in ("/", "\\\\")\n',
            1,
        )
        session_py.write_text(text[:start] + body, encoding="utf-8")

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_float_session_key_indices_and_slices(self):
        cases = (
            ("s[0].isalpha()", "s[0.0].isalpha()"),
            ('s[1] == ":"', 's[1.0] == ":"'),
            ("s[2:3]", "s[2.0:3]"),
            ("s[2:3]", "s[2:3.0]"),
        )
        for old, new in cases:
            with self.subTest(new=new):
                root = self.make_target()
                report = self.patcher.build_report(root, "apply")
                self.assertTrue(report["ok"], report)

                session_py = root / "gateway/session.py"
                text = session_py.read_text(encoding="utf-8")
                start = text.index("def _is_session_key_unsafe")
                body = text[start:].replace(old, new, 1)
                session_py.write_text(text[:start] + body, encoding="utf-8")

                verify_report = self.patcher.build_report(root, "verify")
                self.assertFalse(verify_report["ok"], verify_report)
                self.assertIn(
                    "session.session_key_validator",
                    {
                        item["name"]
                        for item in verify_report["results"]
                        if item["status"] == "missing-structure"
                    },
                )

    def test_verify_rejects_duplicate_session_key_checker(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        insert_at = text.index("\n\n@dataclass", text.index("def _is_session_key_unsafe"))
        duplicate = (
            "\n\n"
            "def _is_session_key_unsafe(value: object) -> bool:\n"
            "    return False\n"
        )
        session_py.write_text(
            text[:insert_at] + duplicate + text[insert_at:],
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_session_key_checker_rebindings(self):
        cases = (
            "_is_session_key_unsafe = lambda value: False\n",
            "(_is_session_key_unsafe := lambda value: False)\n",
            "import math as _is_session_key_unsafe\n",
            "for _is_session_key_unsafe in [lambda value: False]:\n"
            "    pass\n",
            "if True:\n"
            "    _is_session_key_unsafe = lambda value: False\n",
            "del _is_session_key_unsafe\n",
            "try:\n"
            "    raise Exception()\n"
            "except Exception as _is_session_key_unsafe:\n"
            "    pass\n",
            "match (lambda value: False):\n"
            "    case _is_session_key_unsafe:\n"
            "        pass\n",
            'globals()["_is_session_key_unsafe"] = lambda value: False\n',
            'exec("_is_session_key_unsafe = lambda value: False")\n',
        )
        for rebind in cases:
            with self.subTest(rebind=rebind.splitlines()[0]):
                root = self.make_target()
                report = self.patcher.build_report(root, "apply")
                self.assertTrue(report["ok"], report)

                session_py = root / "gateway/session.py"
                text = session_py.read_text(encoding="utf-8")
                insert_at = text.index("\n\n@dataclass", text.index("def _is_session_key_unsafe"))
                session_py.write_text(
                    text[:insert_at] + "\n\n" + rebind + text[insert_at:],
                    encoding="utf-8",
                )

                verify_report = self.patcher.build_report(root, "verify")
                self.assertFalse(verify_report["ok"], verify_report)
                self.assertIn(
                    "session.session_key_validator",
                    {
                        item["name"]
                        for item in verify_report["results"]
                        if item["status"] == "missing-structure"
                    },
                )

    def test_verify_rejects_unbound_session_validation_loop_target(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                "        for _field, _val, _checker in (\n",
                "        for item in (\n",
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.path_sensitive_validation",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_and_instead_of_or_in_session_key_guard(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                '    if ".." in s or "\\\\" in s:\n',
                '    if ".." in s and "\\\\" in s:\n',
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_checker_call_that_does_not_guard_raise(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                "            if _checker(_val):\n"
                "                raise ValueError(\n"
                "                    f\"Invalid {_field}: potential directory traversal detected\"\n"
                "                )\n",
                "            _checker(_val)\n",
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.path_sensitive_validation",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_unreachable_value_error_raise(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                "            if _checker(_val):\n"
                "                raise ValueError(\n"
                "                    f\"Invalid {_field}: potential directory traversal detected\"\n"
                "                )\n",
                "            if _checker(_val):\n"
                "                if False:\n"
                "                    raise ValueError(\n"
                "                        f\"Invalid {_field}: potential directory traversal detected\"\n"
                "                    )\n",
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.path_sensitive_validation",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_or_chain_in_session_key_return(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                '    return len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2:3] in ("/", "\\\\")\n',
                '    return len(s) >= 2 or s[0].isalpha() or s[1] == ":" or s[2:3] in ("/", "\\\\")\n',
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_verify_rejects_nested_or_in_session_key_return(self):
        root = self.make_target()
        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)

        session_py = root / "gateway/session.py"
        text = session_py.read_text(encoding="utf-8")
        session_py.write_text(
            text.replace(
                '    return len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2:3] in ("/", "\\\\")\n',
                '    return len(s) >= 2 and (s[0].isalpha() or s[1] == ":") and s[2:3] in ("/", "\\\\")\n',
            ),
            encoding="utf-8",
        )

        verify_report = self.patcher.build_report(root, "verify")
        self.assertFalse(verify_report["ok"], verify_report)
        self.assertIn(
            "session.session_key_validator",
            {
                item["name"]
                for item in verify_report["results"]
                if item["status"] == "missing-structure"
            },
        )

    def test_apply_preserves_existing_file_mode(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        run_py.chmod(0o640)

        report = self.patcher.build_report(root, "apply")
        self.assertTrue(report["ok"], report)
        self.assertEqual(0o640, stat.S_IMODE(run_py.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
