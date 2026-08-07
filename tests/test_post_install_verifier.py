from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/post_install_verifier.py"
OVERLAY = ROOT / "overlays/hermes"

# check_count of a fully healthy overlay run, measured before the
# no-short-circuit change (2026-07-29). Removing the early-exit gates must not
# swallow any check, so a healthy run stays at or above this number.
HEALTHY_OVERLAY_CHECK_COUNT = 59

# Behaviour/security probes that live behind the former early-exit gates. They
# must be emitted even when an earlier group already failed, otherwise a real
# regression hides as a missing check instead of a failure.
BEHAVIOR_CHECK_NAMES = (
    "plugin.manifest",
    "plugin.entry",
    "plugin.build_source_signature",
    "plugin.runtime_discovery",
    "plugin.raw_process_ack",
    "plugin.reply_context_kwargs",
    "plugin.reply_context_forwarded",
    "product.manifest",
    "product.entry",
    "product.public_hook_contract",
    "gateway.reply_context_layering",
    "gateway.session_key_slash",
    "gateway.session_context_bridge",
)


def load_verifier():
    spec = importlib.util.spec_from_file_location("hermes_dingtalk_post_install_verifier", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PostInstallVerifierTest(unittest.TestCase):
    def setUp(self):
        self.verifier = load_verifier()

    def make_target(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "hermes"
        shutil.copytree(OVERLAY / "gateway", root / "gateway")
        shutil.copytree(
            OVERLAY / "plugins/platforms/dingtalk",
            root / "plugins/platforms/dingtalk",
        )
        shutil.copytree(
            OVERLAY / "plugins/product_confirmation",
            root / "plugins/product_confirmation",
        )
        shutil.copytree(
            OVERLAY / "plugins/h1_task_write",
            root / "plugins/h1_task_write",
        )
        return root

    def test_overlay_installation_report_passes(self):
        report = self.verifier.build_report(OVERLAY)

        self.assertTrue(report["ok"], report)
        self.assertEqual(0, report["failure_count"], report)
        check_names = {item["name"] for item in report["checks"]}
        self.assertIn("compat.verify", check_names)
        self.assertIn("plugin.manifest", check_names)
        self.assertIn("plugin.runtime_discovery", check_names)
        self.assertIn("plugin.raw_process_ack", check_names)
        self.assertIn("plugin.reply_context_kwargs", check_names)
        self.assertIn("plugin.reply_context_forwarded", check_names)
        self.assertIn("product.manifest", check_names)
        self.assertIn("product.entry", check_names)
        self.assertIn("product.public_hook_contract", check_names)
        self.assertIn("h1_task_write.file.__init__.py", check_names)
        self.assertIn("h1_task_write.file.plugin.yaml", check_names)
        self.assertIn("h1_task_write.file.tools.py", check_names)
        self.assertIn("h1_task_write.line_count.tools.py", check_names)
        self.assertIn("gateway.session_key_slash", check_names)
        self.assertIn("gateway.session_context_bridge", check_names)
        self.assertIn("gateway.reply_context_layering", check_names)
        runtime = next(item for item in report["checks"] if item["name"] == "plugin.runtime_discovery")
        self.assertEqual("skipped", runtime["status"])
        self.assertEqual("Hermes runtime modules not present in target root", runtime["message"])
        signature = next(
            item for item in report["checks"] if item["name"] == "plugin.build_source_signature"
        )
        self.assertEqual("skipped", signature["status"])

    REAL_BUILD_SOURCE_SIGNATURE = '''
class BasePlatformAdapter:
    def build_source(
        self,
        chat_id,
        chat_name=None,
        chat_type="dm",
        user_id=None,
        user_name=None,
        thread_id=None,
        chat_topic=None,
        user_id_alt=None,
        chat_id_alt=None,
        is_bot=False,
        guild_id=None,
        parent_chat_id=None,
        message_id=None,
        role_authorized=False,
        auto_thread_created=False,
        auto_thread_initial_name=None,
    ):
        ...
'''

    def make_target_with_core_base(self, base_source: str) -> Path:
        root = self.make_target()
        base_py = root / "gateway/platforms/base.py"
        base_py.parent.mkdir(parents=True, exist_ok=True)
        base_py.write_text(base_source, encoding="utf-8")
        return root

    def test_build_source_signature_probe_passes_with_real_signature(self):
        root = self.make_target_with_core_base(self.REAL_BUILD_SOURCE_SIGNATURE)

        report = self.verifier.build_report(root)

        probe = next(
            item for item in report["checks"] if item["name"] == "plugin.build_source_signature"
        )
        self.assertEqual("ok", probe["status"], probe)

    def test_build_source_signature_probe_fails_on_core_drift(self):
        root = self.make_target_with_core_base(
            self.REAL_BUILD_SOURCE_SIGNATURE.replace("        user_id_alt=None,\n", "")
        )

        report = self.verifier.build_report(root)

        probe = next(
            item for item in report["checks"] if item["name"] == "plugin.build_source_signature"
        )
        self.assertEqual("failed", probe["status"], probe)
        self.assertIn("user_id_alt", probe["message"])

    def make_core_root(self, base_source: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        core = Path(tmp.name) / "core"
        base_py = core / "gateway/platforms/base.py"
        base_py.parent.mkdir(parents=True, exist_ok=True)
        base_py.write_text(base_source, encoding="utf-8")
        return core

    def test_build_source_signature_probe_uses_core_root_override(self):
        root = self.make_target()
        core = self.make_core_root(self.REAL_BUILD_SOURCE_SIGNATURE)

        with mock.patch.dict(os.environ, {"HERMES_DINGTALK_CORE_ROOT": str(core)}):
            report = self.verifier.build_report(root)

        probe = next(
            item for item in report["checks"] if item["name"] == "plugin.build_source_signature"
        )
        self.assertEqual("ok", probe["status"], probe)

    def test_build_source_signature_probe_core_root_override_detects_drift(self):
        root = self.make_target()
        core = self.make_core_root(
            self.REAL_BUILD_SOURCE_SIGNATURE.replace("        user_id_alt=None,\n", "")
        )

        with mock.patch.dict(os.environ, {"HERMES_DINGTALK_CORE_ROOT": str(core)}):
            report = self.verifier.build_report(root)

        probe = next(
            item for item in report["checks"] if item["name"] == "plugin.build_source_signature"
        )
        self.assertEqual("failed", probe["status"], probe)
        self.assertIn("user_id_alt", probe["message"])

    def test_json_cli_is_machine_readable(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--target",
                str(OVERLAY),
                "--json",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"], report)
        self.assertEqual(0, report["failure_count"], report)
        self.assertIn("checks", report)

    def test_missing_plugin_module_fails_without_source_dump(self):
        root = self.make_target()
        (root / "plugins/platforms/dingtalk/reply_context.py").unlink()

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        failures = [item for item in report["checks"] if item["status"] != "ok"]
        self.assertTrue(
            any(item["name"] == "plugin.file.reply_context.py" for item in failures),
            failures,
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("_REPLY_ORIGINAL_UNAVAILABLE =", serialized)
        self.assertNotIn("class DingTalkAdapter", serialized)
        check_names = {item["name"] for item in report["checks"]}
        self.assertIn("plugin.manifest", check_names)
        self.assertIn("plugin.entry", check_names)
        self.assertIn("plugin.reply_context_kwargs", check_names)
        self.assertIn("plugin.reply_context_forwarded", check_names)
        self.assertIn("gateway.session_key_slash", check_names)

    def test_missing_plugin_manifest_still_emits_behavior_probes(self):
        root = self.make_target()
        (root / "plugins/platforms/dingtalk/plugin.yaml").unlink()

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        failures = [item for item in report["checks"] if item["status"] != "ok"]
        self.assertTrue(
            any(item["name"] == "plugin.file.plugin.yaml" for item in failures),
            failures,
        )
        check_names = {item["name"] for item in report["checks"]}
        self.assertIn("plugin.manifest", check_names)
        self.assertIn("plugin.entry", check_names)
        self.assertIn("plugin.runtime_discovery", check_names)
        self.assertIn("plugin.raw_process_ack", check_names)

    def test_runtime_probe_env_does_not_inherit_secrets(self):
        root = self.make_target()
        with tempfile.TemporaryDirectory() as temp_home:
            old_environ = os.environ.copy()
            self.addCleanup(os.environ.update, old_environ)
            self.addCleanup(os.environ.clear)
            os.environ.clear()
            os.environ.update(
                {
                    "PATH": "/usr/bin:/bin",
                    "OPENAI_API_KEY": "redacted-openai-key-placeholder",
                    "DINGTALK_CLIENT_SECRET": "secret-that-must-not-propagate",
                    "HERMES_SAFE_MODE": "1",
                    "PYTHONPATH": "/tmp/parent-pythonpath",
                }
            )

            env = self.verifier._runtime_probe_env(root, temp_home)

        self.assertEqual("/usr/bin:/bin", env["PATH"])
        self.assertEqual(temp_home, env["HERMES_HOME"])
        self.assertEqual(str(root / "plugins"), env["HERMES_BUNDLED_PLUGINS"])
        self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual(str(root), env["PYTHONPATH"])
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("DINGTALK_CLIENT_SECRET", env)
        self.assertNotIn("HERMES_SAFE_MODE", env)

    def test_product_hook_accepts_official_getattr_session_store_shape(self):
        root = self.make_target()
        registry = root / "hermes_cli/plugins.py"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            'VALID_HOOKS = {"pre_gateway_dispatch"}\n',
            encoding="utf-8",
        )
        run_py = root / "gateway/run.py"
        run_py.write_text(
            run_py.read_text(encoding="utf-8").replace(
                "session_store=self.session_store,",
                'session_store=getattr(self, "session_store", None),',
                1,
            ),
            encoding="utf-8",
        )

        result = self.verifier._product_hook_contract_probe(root)

        self.assertEqual("ok", result.status, result)

    def test_gateway_structure_failure_is_reported_from_compat_verify(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        run_py.write_text(
            text.replace("            session_id=context.session_id,\n", ""),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        failures = [item for item in report["checks"] if item["status"] != "ok"]
        self.assertTrue(
            any(item["name"] == "compat.run.session_env_fields" for item in failures),
            failures,
        )
        check_names = {item["name"] for item in report["checks"]}
        self.assertIn("plugin.raw_process_ack", check_names)
        self.assertIn("plugin.reply_context_kwargs", check_names)
        self.assertIn("gateway.session_key_slash", check_names)

    def test_reply_context_forwarding_without_kwargs_expansion_fails(self):
        root = self.make_target()
        adapter = root / "plugins/platforms/dingtalk/adapter.py"
        text = adapter.read_text(encoding="utf-8")
        self.assertIn("            **reply_kwargs,\n", text)
        adapter.write_text(
            text.replace("            **reply_kwargs,\n", "", 1),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "plugin.reply_context_forwarded"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("does not expand reply_kwargs", failure["message"])

    def test_extra_reply_context_policy_in_adapter_fails(self):
        root = self.make_target()
        adapter = root / "plugins/platforms/dingtalk/adapter.py"
        text = adapter.read_text(encoding="utf-8")
        marker = "        reply_kwargs = build_reply_kwargs(message)\n"
        self.assertIn(marker, text)
        adapter.write_text(
            text.replace(
                marker,
                marker
                + '        if reply_kwargs.get("reply_to_text") == _REPLY_ORIGINAL_UNAVAILABLE:\n'
                + "            return\n",
                1,
            ),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "plugin.reply_context_forwarded"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("unexpected reply policy branch", failure["message"])

    def test_literal_reply_context_guard_in_adapter_fails(self):
        root = self.make_target()
        adapter = root / "plugins/platforms/dingtalk/adapter.py"
        text = adapter.read_text(encoding="utf-8")
        marker = "        reply_kwargs = build_reply_kwargs(message)\n"
        self.assertIn(marker, text)
        adapter.write_text(
            text.replace(
                marker,
                marker
                + '        if reply_kwargs.get("reply_to_text") == "\\\\x00unavailable\\\\x00":\n'
                + "            return\n",
                1,
            ),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "plugin.reply_context_forwarded"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("unexpected reply policy branch", failure["message"])

    def test_card_reply_recovery_without_unknown_return_fails(self):
        root = self.make_target()
        adapter = root / "plugins/platforms/dingtalk/adapter.py"
        text = adapter.read_text(encoding="utf-8")
        marker = "                return\n\n        event = MessageEvent(\n"
        self.assertIn(marker, text)
        adapter.write_text(
            text.replace(marker, "\n        event = MessageEvent(\n", 1),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "plugin.reply_context_forwarded"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("does not stop before model dispatch", failure["message"])

    def test_gateway_reply_context_without_return_none_fails(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = "                    return None\n                message_text = (\n"
        self.assertIn(marker, text)
        run_py.write_text(
            text.replace(marker, "                message_text = (\n", 1),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("did not clarify once and return None", failure["message"])

    def test_gateway_reply_context_caller_tool_before_stop_fails(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = "        if message_text is None:\n            return\n"
        self.assertEqual(1, text.count(marker))
        run_py.write_text(
            text.replace(
                marker,
                "        if message_text is None:\n"
                "            await self._dispatch_business_tool_before_stop()\n"
                "            return\n",
                1,
            ),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("prepare callers are not all fail-closed", failure["message"])

    def test_gateway_reply_context_caller_return_expression_call_fails(self):
        mutations = (
            (
                "        if message_text is None:\n            return\n",
                "        if message_text is None:\n"
                "            return await self._dispatch_business_tool_before_stop()\n",
            ),
            (
                "                    if next_message is None:\n"
                "                        return result\n",
                "                    if next_message is None:\n"
                "                        return await self._dispatch_business_tool_before_stop()\n",
            ),
        )
        for marker, replacement in mutations:
            with self.subTest(marker=marker):
                root = self.make_target()
                run_py = root / "gateway/run.py"
                text = run_py.read_text(encoding="utf-8")
                self.assertEqual(1, text.count(marker))
                run_py.write_text(
                    text.replace(marker, replacement, 1),
                    encoding="utf-8",
                )

                report = self.verifier.build_report(root)

                failure = next(
                    item
                    for item in report["checks"]
                    if item["name"] == "gateway.reply_context_layering"
                )
                self.assertEqual("failed", failure["status"])
                self.assertIn("prepare callers are not all fail-closed", failure["message"])

    def test_gateway_reply_context_caller_prepare_call_side_effects_fail(self):
        mutations = (
            (
                "message_text = await self._prepare_inbound_message_text(",
                "message_text = await decoy._prepare_inbound_message_text(",
            ),
            (
                "next_message = await self._prepare_inbound_message_text(",
                "next_message = await decoy._prepare_inbound_message_text(",
            ),
            (
                "message_text = await self._prepare_inbound_message_text(\n"
                "            event=event,\n",
                "message_text = await self._prepare_inbound_message_text(\n"
                "            event=await self._dispatch_business_tool_before_stop(),\n",
            ),
            (
                "next_message = await self._prepare_inbound_message_text(\n"
                "                        event=pending_event,\n",
                "next_message = await self._prepare_inbound_message_text(\n"
                "                        event=self._dispatch_business_tool_before_stop(),\n",
            ),
        )
        for marker, replacement in mutations:
            with self.subTest(marker=marker):
                root = self.make_target()
                run_py = root / "gateway/run.py"
                text = run_py.read_text(encoding="utf-8")
                self.assertEqual(1, text.count(marker))
                run_py.write_text(
                    text.replace(marker, replacement, 1),
                    encoding="utf-8",
                )

                report = self.verifier.build_report(root)

                failure = next(
                    item
                    for item in report["checks"]
                    if item["name"] == "gateway.reply_context_layering"
                )
                self.assertEqual("failed", failure["status"])
                self.assertIn("prepare callers are not all fail-closed", failure["message"])

    def test_gateway_reply_context_caller_semantic_binding_mutations_fail(self):
        mutations = (
            (
                (
                    "            event=event,\n"
                    "            source=source,\n"
                    "            history=history,\n",
                    "            event=history,\n"
                    "            source=source,\n"
                    "            history=event,\n",
                ),
            ),
            (
                (
                    "                        event=pending_event,\n"
                    "                        source=next_source,\n"
                    "                        history=updated_history,\n",
                    "                        event=updated_history,\n"
                    "                        source=next_source,\n"
                    "                        history=pending_event,\n",
                ),
            ),
            (
                (
                    "message_text = await self._prepare_inbound_message_text(",
                    "prepared_decoy = await self._prepare_inbound_message_text(",
                ),
                (
                    "        if message_text is None:\n",
                    "        if prepared_decoy is None:\n",
                ),
            ),
            (
                (
                    "next_message = await self._prepare_inbound_message_text(",
                    "prepared_decoy = await self._prepare_inbound_message_text(",
                ),
                (
                    "                    if next_message is None:\n",
                    "                    if prepared_decoy is None:\n",
                ),
            ),
        )
        for replacements in mutations:
            with self.subTest(replacements=replacements):
                root = self.make_target()
                run_py = root / "gateway/run.py"
                text = run_py.read_text(encoding="utf-8")
                for marker, replacement in replacements:
                    self.assertEqual(1, text.count(marker))
                    text = text.replace(marker, replacement, 1)
                run_py.write_text(text, encoding="utf-8")

                report = self.verifier.build_report(root)

                failure = next(
                    item
                    for item in report["checks"]
                    if item["name"] == "gateway.reply_context_layering"
                )
                self.assertEqual("failed", failure["status"])
                self.assertIn("prepare callers are not all fail-closed", failure["message"])

    def test_gateway_reply_context_caller_unreachable_mutations_fail(self):
        cases = (
            (
                "first_dead",
                "        message_text = await self._prepare_inbound_message_text(",
                "\n\n        # Capture the platform event time",
            ),
            (
                "second_dead",
                "                    next_message = await self._prepare_inbound_message_text(",
                "                    next_message_id = self._reply_anchor_for_event",
            ),
            (
                "first_return",
                "        message_text = await self._prepare_inbound_message_text(",
                None,
            ),
            (
                "second_return",
                "                    next_message = await self._prepare_inbound_message_text(",
                None,
            ),
        )
        for name, start_marker, end_marker in cases:
            with self.subTest(name=name):
                root = self.make_target()
                run_py = root / "gateway/run.py"
                text = run_py.read_text(encoding="utf-8")
                self.assertEqual(1, text.count(start_marker))
                if name.endswith("_dead"):
                    start = text.index(start_marker)
                    end = text.index(end_marker, start)
                    block = text[start:end]
                    indent = start_marker[: len(start_marker) - len(start_marker.lstrip())]
                    replacement = indent + "if False:\n" + textwrap.indent(block, "    ")
                    text = text[:start] + replacement + text[end:]
                else:
                    early_return = (
                        "        return\n"
                        if name == "first_return"
                        else "                    return result\n"
                    )
                    text = text.replace(
                        start_marker,
                        early_return + start_marker,
                        1,
                    )
                run_py.write_text(text, encoding="utf-8")

                report = self.verifier.build_report(root)

                failure = next(
                    item
                    for item in report["checks"]
                    if item["name"] == "gateway.reply_context_layering"
                )
                self.assertEqual("failed", failure["status"])
                self.assertIn("prepare callers are not all fail-closed", failure["message"])

    def test_gateway_reply_context_caller_parent_path_mutations_fail(self):
        cases = (
            (
                "first_wrapper",
                "        message_text = await self._prepare_inbound_message_text(",
                "\n\n        # Capture the platform event time",
                "event is None",
            ),
            (
                "second_wrapper",
                "                    next_message = await self._prepare_inbound_message_text(",
                "                    next_message_id = self._reply_anchor_for_event",
                "pending_event is None",
            ),
            (
                "first_assert",
                "        message_text = await self._prepare_inbound_message_text(",
                None,
                None,
            ),
            (
                "second_assert",
                "                    next_message = await self._prepare_inbound_message_text(",
                None,
                None,
            ),
        )
        for name, start_marker, end_marker, condition in cases:
            with self.subTest(name=name):
                root = self.make_target()
                run_py = root / "gateway/run.py"
                text = run_py.read_text(encoding="utf-8")
                self.assertEqual(1, text.count(start_marker))
                indent = start_marker[: len(start_marker) - len(start_marker.lstrip())]
                if name.endswith("_wrapper"):
                    start = text.index(start_marker)
                    end = text.index(end_marker, start)
                    block = text[start:end]
                    replacement = (
                        indent
                        + f"if {condition}:\n"
                        + textwrap.indent(block, "    ")
                    )
                    text = text[:start] + replacement + text[end:]
                else:
                    text = text.replace(
                        start_marker,
                        indent + "assert False\n" + start_marker,
                        1,
                    )
                run_py.write_text(text, encoding="utf-8")

                report = self.verifier.build_report(root)

                failure = next(
                    item
                    for item in report["checks"]
                    if item["name"] == "gateway.reply_context_layering"
                )
                self.assertEqual("failed", failure["status"])
                self.assertIn("prepare callers are not all fail-closed", failure["message"])

    def test_gateway_reply_context_core_session_key_call_shape_passes(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        first = (
            "        message_text = await self._prepare_inbound_message_text(\n"
            "            event=event,\n"
            "            source=source,\n"
            "            history=history,\n"
            "        )\n"
        )
        second = (
            "                    next_message = await self._prepare_inbound_message_text(\n"
            "                        event=pending_event,\n"
            "                        source=next_source,\n"
            "                        history=updated_history,\n"
            "                    )\n"
        )
        self.assertEqual(1, text.count(first))
        self.assertEqual(1, text.count(second))
        text = text.replace(
            first,
            first.replace(
                "            history=history,\n",
                "            history=history,\n"
                "            session_key=session_key,\n",
            ),
            1,
        )
        text = text.replace(
            second,
            second.replace(
                "                        history=updated_history,\n",
                "                        history=updated_history,\n"
                "                        session_key=next_session_key,\n",
            ),
            1,
        )
        run_py.write_text(text, encoding="utf-8")

        report = self.verifier.build_report(root)

        check = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("ok", check["status"], report)

    def test_gateway_reply_context_duplicate_decoy_branch_fails(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
        self.assertEqual(1, text.count(marker))
        run_py.write_text(
            text.replace(
                marker,
                marker + "                pass\n" + marker,
                1,
            ),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("not unique on the direct Gateway reply path", failure["message"])

    def test_gateway_reply_context_single_branch_under_dead_wrapper_fails(self):
        root = self.make_target()
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

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        compat_failure = next(
            item
            for item in report["checks"]
            if item["name"] == "compat.run.reply_context_sentinel_branch"
        )
        self.assertEqual("missing-structure", compat_failure["status"])
        layer_failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", layer_failure["status"])
        self.assertIn(
            "not unique on the direct Gateway reply path",
            layer_failure["message"],
        )

    def test_gateway_reply_context_preceding_method_return_fails(self):
        root = self.make_target()
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

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        compat_failure = next(
            item
            for item in report["checks"]
            if item["name"] == "compat.run.reply_context_sentinel_branch"
        )
        self.assertEqual("missing-structure", compat_failure["status"])
        layer_failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", layer_failure["status"])
        self.assertIn(
            "not unique on the direct Gateway reply path",
            layer_failure["message"],
        )

    def test_gateway_reply_context_prefix_reply_state_mutation_fails(self):
        root = self.make_target()
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

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        compat_failure = next(
            item
            for item in report["checks"]
            if item["name"] == "compat.run.reply_context_sentinel_branch"
        )
        self.assertEqual("missing-structure", compat_failure["status"])
        layer_failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", layer_failure["status"])
        self.assertIn(
            "not unique on the direct Gateway reply path",
            layer_failure["message"],
        )

    def test_gateway_reply_context_with_weakened_prompt_fails(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = "只有当引用目标唯一明确时才能继续处理"
        self.assertIn(marker, text)
        run_py.write_text(
            text.replace(marker, "优先参考最近的话题继续处理", 1),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("complete V2 contract", failure["message"])

    def test_gateway_reply_context_silent_send_failure_fails(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = '                            if not getattr(_reply_result, "success", False):\n'
        self.assertIn(marker, text)
        run_py.write_text(
            text.replace(marker, "                            if False:\n", 1),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("did not stay closed with WARNING", failure["message"])

    def test_gateway_reply_context_silent_send_exception_fails(self):
        root = self.make_target()
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        marker = (
            "                        except Exception:\n"
            "                            logger.warning(\n"
        )
        self.assertIn(marker, text)
        run_py.write_text(
            text.replace(
                marker,
                "                        except Exception:\n"
                "                            logger.warning = lambda *args, **kwargs: None\n"
                "                            logger.warning(\n",
                1,
            ),
            encoding="utf-8",
        )

        report = self.verifier.build_report(root)

        failure = next(
            item
            for item in report["checks"]
            if item["name"] == "gateway.reply_context_layering"
        )
        self.assertEqual("failed", failure["status"])
        self.assertIn("exception did not stay closed with WARNING", failure["message"])

    def break_compat_section(self, root: Path) -> None:
        run_py = root / "gateway/run.py"
        text = run_py.read_text(encoding="utf-8")
        run_py.write_text(
            text.replace("            session_id=context.session_id,\n", ""),
            encoding="utf-8",
        )

    def test_compat_failure_does_not_suppress_later_checks(self):
        root = self.make_target()
        self.break_compat_section(root)

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        check_names = {item["name"] for item in report["checks"]}
        self.assertTrue(
            any(item["name"] == "compat.run.session_env_fields" for item in report["checks"]),
            report,
        )
        for name in BEHAVIOR_CHECK_NAMES:
            self.assertIn(name, check_names)
        self.assertIn("plugin.file.adapter.py", check_names)
        self.assertIn("product.file.plugin.yaml", check_names)
        self.assertIn("h1_task_write.file.tools.py", check_names)

    def test_missing_plugin_file_does_not_suppress_behavior_checks(self):
        root = self.make_target()
        (root / "plugins/platforms/dingtalk/reply_context.py").unlink()

        report = self.verifier.build_report(root)

        self.assertFalse(report["ok"], report)
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual("missing-file", checks["plugin.file.reply_context.py"]["status"])
        for name in BEHAVIOR_CHECK_NAMES:
            self.assertIn(name, checks)
        # The probe that depends on the deleted file must surface as an explicit
        # error result, not vanish from the report.
        self.assertEqual("error", checks["plugin.reply_context_kwargs"]["status"])
        self.assertIn("FileNotFoundError", checks["plugin.reply_context_kwargs"]["message"])

    def test_healthy_overlay_keeps_full_check_count(self):
        report = self.verifier.build_report(OVERLAY)

        self.assertTrue(report["ok"], report)
        self.assertEqual(0, report["failure_count"], report)
        self.assertGreaterEqual(report["check_count"], HEALTHY_OVERLAY_CHECK_COUNT, report)
        self.assertEqual(report["check_count"], len(report["checks"]), report)

    def test_group_producer_exception_becomes_an_explicit_failure(self):
        def boom() -> list:
            raise RuntimeError("group blew up")

        results = self.verifier._run_group("plugin.files", "plugins", boom)

        self.assertEqual(1, len(results))
        self.assertEqual("error", results[0].status)
        self.assertIn("RuntimeError: group blew up", results[0].message)


if __name__ == "__main__":
    unittest.main()
