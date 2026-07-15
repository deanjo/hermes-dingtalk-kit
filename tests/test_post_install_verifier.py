from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/post_install_verifier.py"
OVERLAY = ROOT / "overlays/hermes"


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
        self.assertIn("product.manifest", check_names)
        self.assertIn("product.entry", check_names)
        self.assertIn("product.public_hook_contract", check_names)
        self.assertIn("gateway.session_key_slash", check_names)
        self.assertIn("gateway.session_context_bridge", check_names)
        runtime = next(item for item in report["checks"] if item["name"] == "plugin.runtime_discovery")
        self.assertEqual("skipped", runtime["status"])
        self.assertEqual("Hermes runtime modules not present in target root", runtime["message"])

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
        self.assertNotIn("plugin.manifest", check_names)
        self.assertNotIn("plugin.entry", check_names)
        self.assertNotIn("plugin.reply_context_kwargs", check_names)
        self.assertNotIn("gateway.session_key_slash", check_names)

    def test_missing_plugin_manifest_fails_before_behavior_probes(self):
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
        self.assertNotIn("plugin.manifest", check_names)
        self.assertNotIn("plugin.entry", check_names)
        self.assertNotIn("plugin.runtime_discovery", check_names)
        self.assertNotIn("plugin.raw_process_ack", check_names)

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
        self.assertNotIn("plugin.raw_process_ack", check_names)
        self.assertNotIn("plugin.reply_context_kwargs", check_names)
        self.assertNotIn("gateway.session_key_slash", check_names)


if __name__ == "__main__":
    unittest.main()
