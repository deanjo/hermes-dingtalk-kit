from pathlib import Path
import ast
import importlib.util
import shutil
import stat
import sys
import tempfile
import textwrap
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


def _revert_step(root: Path, step) -> None:
    """Revert one step's patched form back to its pre-patch anchor so the
    patcher can re-apply it.  Needed because the 2026-07-24 baseline
    refresh made the shipped gateway files already fully patched."""
    path = root / step.path
    text = path.read_text(encoding="utf-8")
    for old, new in step.replacements():
        if new in text:
            path.write_text(text.replace(new, old, 1), encoding="utf-8")
            return
    raise AssertionError(f"{step.name}: no patched variant found in {step.path}")


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

        # The fixture still carries the deployed V1 reply-context patch. A
        # first pass must migrate that one step to V2; a second pass is a no-op.
        check_report = self.patcher.build_report(root, "check")
        self.assertTrue(check_report["ok"], check_report)
        self.assertEqual(1, check_report["changed_count"], check_report)
        first_apply = self.patcher.build_report(root, "apply")
        self.assertTrue(first_apply["ok"], first_apply)
        self.assertEqual(["gateway/run.py"], first_apply["changed_files"])
        second_apply = self.patcher.build_report(root, "apply")
        self.assertTrue(second_apply["ok"], second_apply)
        self.assertEqual(0, second_apply["changed_count"], second_apply)

        # Revert every step to its pre-patch anchor: the patcher must then
        # re-apply exactly the three files, and no-op on the second pass.
        for step in self.patcher.STEPS:
            _revert_step(root, step)
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
        # The refreshed baseline is already patched: damaging the marker
        # line itself makes the step unresolvable (no marker match, and the
        # pre-patch anchor is gone from a patched tree) — it must stop
        # without writing anything.
        run_py.write_text(
            original.replace(
                "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n", ""
            ),
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
        # Normalize the deployed V1 fixture to V2 before reverting selected
        # steps; rollback testing starts from one fully patched shape.
        initial = self.patcher.build_report(root, "apply")
        self.assertTrue(initial["ok"], initial)
        # Give the patcher real work first: revert the run.py and session.py
        # steps to their pre-patch anchors so an apply must write both files.
        for step in self.patcher.STEPS:
            if step.path in {"gateway/run.py", "gateway/session.py"}:
                _revert_step(root, step)
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


class CompatNativeShapeTest(unittest.TestCase):
    """H1 治理第 6 项新增机制的用例（native_marker / ifs 谓词 / 新替换对）。

    这些机制让补丁认得 fork core 已原生实现的等价形态。它们一旦失效，
    表现是"补丁静默跳过"或"假绿"——正是发布链挂了两周的那类故障。
    """

    def _mod(self):
        return load_patcher()

    def test_native_marker_absent_keeps_original_behaviour(self):
        """未配 native_marker 的步骤，行为必须与引入该字段前逐字一致。"""
        cp = self._mod()
        for step in cp.STEPS:
            if step.native_marker is None:
                text = "".join(step.markers())
                self.assertEqual(
                    step.already_satisfied(text),
                    cp._all_present(text, step.markers()),
                    f"{step.name} 的 already_satisfied 偏离了原 marker 语义",
                )

    def test_native_marker_recognises_core_native_shape(self):
        """core 原生形态命中 native_marker 即判 present（补丁冗余）。"""
        cp = self._mod()
        step = next(s for s in cp.STEPS if s.name == "session.path_sensitive_validation")
        self.assertIsNotNone(step.native_marker)
        native_text = "\n".join(step.native_markers())
        self.assertTrue(step.already_satisfied(native_text))

    def test_ifs_predicate_requires_same_function_scope(self):
        """两个 if 分散在不同函数里不算数——防 decoy 绕过（独立验收 P1）。"""
        cp = self._mod()
        split_scopes = (
            "def a(data):\n"
            "    session_id = data['session_id']\n"
            "    if _is_path_unsafe(session_id):\n"
            "        raise ValueError('x')\n"
            "def b(data):\n"
            "    session_key = data['session_key']\n"
            "    if _is_session_key_unsafe(session_key):\n"
            "        raise ValueError('y')\n"
        )
        self.assertFalse(cp._has_session_path_validation_ifs(ast.parse(split_scopes)))

    def test_ifs_predicate_rejects_dead_decoy_function(self):
        """从未取值的同形 decoy 函数不得让 verify 变绿（独立验收 P1 实测绕过）。

        校验必须长在真正从数据取 session_key/session_id 的地方；只要求"同一
        函数"是不够的——decoy 自己就是一个函数。
        """
        cp = self._mod()
        decoy = (
            "def _dead_decoy(session_id, session_key):\n"
            "    if _is_path_unsafe(session_id):\n"
            "        raise ValueError('x')\n"
            "    if _is_session_key_unsafe(session_key):\n"
            "        raise ValueError('y')\n"
        )
        self.assertFalse(cp._has_session_path_validation_ifs(ast.parse(decoy)))

    def test_ifs_predicate_accepts_real_construction_site(self):
        cp = self._mod()
        real_site = (
            "def from_dict(data):\n"
            "    session_key = data['session_key']\n"
            "    session_id = data['session_id']\n"
            "    if _is_path_unsafe(session_id):\n"
            "        raise ValueError('x')\n"
            "    if _is_session_key_unsafe(session_key):\n"
            "        raise ValueError('y')\n"
        )
        self.assertTrue(cp._has_session_path_validation_ifs(ast.parse(real_site)))

    def test_ifs_predicate_rejects_swapped_checkers(self):
        """checker 张冠李戴必须判否——谓词查的是语义不是形状。"""
        cp = self._mod()
        swapped = (
            "def loader(data):\n"
            "    session_key = data['session_key']\n"
            "    session_id = data['session_id']\n"
            "    if _is_session_key_unsafe(session_id):\n"
            "        raise ValueError('x')\n"
            "    if _is_path_unsafe(session_key):\n"
            "        raise ValueError('y')\n"
        )
        self.assertFalse(cp._has_session_path_validation_ifs(ast.parse(swapped)))

    def test_native_old_anchors_are_unique_in_core(self):
        """两个新替换对的 old 锚点必须在 core 中唯一，否则替换会打错地方。"""
        cp = self._mod()
        core = Path(
            "/Users/cicada/SourceCode/openclaw-hermes-workspace/hermes-workspace"
            "/worktrees/hermes-agent-t5e-release-integration-20260720"
        )
        run_py = core / "gateway/run.py"
        session_py = core / "gateway/session.py"
        if not run_py.exists() or not session_py.exists():
            self.skipTest("core worktree 不在场")
        self.assertEqual(run_py.read_text(encoding="utf-8").count(cp._REPLY_CTX_NATIVE_OLD), 1)
        self.assertEqual(
            session_py.read_text(encoding="utf-8").count(cp._SESSION_KEY_CHECKER_NATIVE_OLD), 1
        )

    def test_reply_context_marker_distinguishes_v1_from_v2(self):
        """V1 不能被误判 present，否则已部署实例永远升不到 V2。"""
        cp = self._mod()
        step = next(s for s in cp.STEPS if s.name == "run.reply_context_sentinel_branch")
        self.assertIn("只有当引用目标唯一明确时才能继续处理", step.markers())
        self.assertFalse(step.already_satisfied(cp._REPLY_CTX_NATIVE_OLD))
        self.assertFalse(step.already_satisfied(cp._REPLY_CTX_LEGACY_V1))
        self.assertFalse(step.already_satisfied(cp._REPLY_CTX_NATIVE_V1))
        self.assertTrue(step.already_satisfied(cp._REPLY_CTX_LEGACY_V2))
        self.assertTrue(step.already_satisfied(cp._REPLY_CTX_NATIVE_V2))

    def test_reply_context_replacements_cover_old_and_v1_shapes(self):
        cp = self._mod()
        step = next(s for s in cp.STEPS if s.name == "run.reply_context_sentinel_branch")
        replacements = dict(step.replacements())
        self.assertEqual(cp._REPLY_CTX_LEGACY_V2, replacements[cp._REPLY_CTX_LEGACY_OLD])
        self.assertEqual(cp._REPLY_CTX_NATIVE_V2, replacements[cp._REPLY_CTX_NATIVE_OLD])
        self.assertEqual(cp._REPLY_CTX_LEGACY_V2, replacements[cp._REPLY_CTX_LEGACY_V1])
        self.assertEqual(cp._REPLY_CTX_NATIVE_V2, replacements[cp._REPLY_CTX_NATIVE_V1])

    @staticmethod
    def _parse_reply_v2(cp, source: str):
        body = textwrap.dedent(source)
        if 'if getattr(event, "reply_to_text", None)' not in body:
            body = (
                'if getattr(event, "reply_to_text", None) and event.reply_to_message_id:\n'
                + textwrap.indent(body, "    ")
            )
        wrapped = (
            "class GatewayRunner:\n"
            "    async def _prepare_inbound_message_text("
            "self, event, source, history, message_text):\n"
            + textwrap.indent(body, "        ")
        )
        return ast.parse(wrapped)

    def test_reply_context_v2_verifier_rejects_safety_mutations(self):
        cp = self._mod()
        source = cp._REPLY_CTX_NATIVE_V2
        self.assertTrue(
            cp._NATIVE_SHAPES.has_reply_context_v2(
                self._parse_reply_v2(cp, source),
                source,
            )
        )
        mutations = {
            "no-return": source.replace("                    return None\n", "", 1),
            "wrong-history-role": source.replace(
                'item.get("role") == "assistant"',
                'item.get("role") == "user"',
                1,
            ),
            "empty-assistant-counts": source.replace(
                '                    and bool(str(item.get("content") or "").strip())\n',
                "",
                1,
            ),
            "no-send": source.replace(
                "                            _reply_result = await _reply_adapter.send(\n",
                "                            _reply_result = None\n                            noop(\n",
                1,
            ),
            "weak-prompt": source.replace(
                "只有当引用目标唯一明确时才能继续处理",
                "请参考最近的话题继续",
                1,
            ),
        }
        for name, mutated in mutations.items():
            with self.subTest(name=name):
                self.assertFalse(
                    cp._NATIVE_SHAPES.has_reply_context_v2(
                        self._parse_reply_v2(cp, mutated),
                        mutated,
                    )
                )

    def test_reply_context_verifier_rejects_single_branch_under_dead_wrapper(self):
        cp = self._mod()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "hermes"
        shutil.copytree(BASELINE / "gateway", root / "gateway")
        apply_report = cp.build_report(root, "apply")
        self.assertTrue(apply_report["ok"], apply_report)
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        start_marker = (
            "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
        )
        end_marker = '\n        if "@" in message_text:'
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        live_branch = text[start:end]
        run_py.write_text(
            text[:start]
            + "            if False:\n"
            + textwrap.indent(live_branch, "    ")
            + text[end:],
            encoding="utf-8",
        )

        report = cp.build_report(root, "verify")

        self.assertFalse(report["ok"], report)
        failure = next(
            item
            for item in report["results"]
            if item["name"] == "run.reply_context_sentinel_branch"
        )
        self.assertEqual("missing-structure", failure["status"])

    def test_reply_context_verifier_rejects_preceding_method_return(self):
        cp = self._mod()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "hermes"
        shutil.copytree(BASELINE / "gateway", root / "gateway")
        apply_report = cp.build_report(root, "apply")
        self.assertTrue(apply_report["ok"], apply_report)
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = (
            '        if getattr(event, "reply_to_text", None) '
            "and event.reply_to_message_id:\n"
        )
        self.assertEqual(1, text.count(marker))
        run_py.write_text(
            text.replace(marker, "        return message_text\n" + marker, 1),
            encoding="utf-8",
        )

        report = cp.build_report(root, "verify")

        self.assertFalse(report["ok"], report)
        failure = next(
            item
            for item in report["results"]
            if item["name"] == "run.reply_context_sentinel_branch"
        )
        self.assertEqual("missing-structure", failure["status"])

    def test_reply_context_verifier_rejects_prefix_reply_state_mutation(self):
        cp = self._mod()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "hermes"
        shutil.copytree(BASELINE / "gateway", root / "gateway")
        apply_report = cp.build_report(root, "apply")
        self.assertTrue(apply_report["ok"], apply_report)
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = (
            '        if getattr(event, "reply_to_text", None) '
            "and event.reply_to_message_id:\n"
        )
        self.assertEqual(1, text.count(marker))
        run_py.write_text(
            text.replace(marker, "        event.reply_to_text = None\n" + marker, 1),
            encoding="utf-8",
        )

        report = cp.build_report(root, "verify")

        self.assertFalse(report["ok"], report)
        failure = next(
            item
            for item in report["results"]
            if item["name"] == "run.reply_context_sentinel_branch"
        )
        self.assertEqual("missing-structure", failure["status"])

    def test_session_key_marker_distinguishes_implementation(self):
        """marker 必须认实现而非仅函数名，否则 core 弱实现会被误判为已打补丁。"""
        cp = self._mod()
        step = next(s for s in cp.STEPS if s.name == "session.session_key_validator")
        self.assertFalse(step.already_satisfied(cp._SESSION_KEY_CHECKER_NATIVE_OLD))
        self.assertTrue(step.already_satisfied(cp.CANONICAL_SESSION_KEY_CHECKER))

    def test_core_native_session_key_checker_is_weaker(self):
        """钉住"core 弱两处"这个判断本身：中间反斜杠与开头 ~ 必须是真实差异。"""
        cp = self._mod()
        ns: dict = {}
        exec(cp._SESSION_KEY_CHECKER_NATIVE_OLD, ns)
        core_fn = ns["_is_session_key_unsafe"]
        ns2: dict = {}
        exec(cp.CANONICAL_SESSION_KEY_CHECKER, ns2)
        canon_fn = ns2["_is_session_key_unsafe"]
        for probe in ("a\\b", "~/.ssh/id_rsa"):
            self.assertFalse(core_fn(probe), f"core 竟拦住了 {probe!r}，'弱两处'的判断需复核")
            self.assertTrue(canon_fn(probe), f"canonical 未拦住 {probe!r}")
