from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".baseline/hermes"
SCRIPT = ROOT / "scripts/install_dingtalk_kit.py"
DOCKERFILE = ROOT / "docker/Dockerfile"
EXPECTED_VERIFIER_CHECKS = 38


def load_installer():
    spec = importlib.util.spec_from_file_location("hermes_dingtalk_installer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class InstallDingTalkKitTest(unittest.TestCase):
    def setUp(self):
        self.installer = load_installer()

    def make_target(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "hermes"
        shutil.copytree(BASELINE / "gateway", root / "gateway")
        return root

    def snapshot_core(self, root: Path) -> dict[str, bytes]:
        return {
            rel_path: (root / rel_path).read_bytes()
            for rel_path in (
                "gateway/run.py",
                "gateway/session.py",
                "gateway/session_context.py",
            )
            if (root / rel_path).exists()
        }

    def test_installs_into_baseline_and_verifier_passes(self):
        root = self.make_target()

        report = self.installer.build_report(root)

        self.assertTrue(report["ok"], report)
        self.assertEqual(0, report["failure_count"], report)
        self.assertEqual(
            ["target.resolve", "plugin.source", "plugin.copy", "compat.apply", "post_install.verify"],
            [item["name"] for item in report["operations"]],
        )
        self.assertEqual(3, report["compat"]["changed_count"], report)
        self.assertEqual(EXPECTED_VERIFIER_CHECKS, report["verifier"]["check_count"], report)
        self.assertEqual(0, report["verifier"]["failure_count"], report)
        self.assertTrue((root / "plugins/platforms/dingtalk/adapter.py").is_file())
        self.assertTrue((root / "plugins/platforms/dingtalk/__init__.py").is_file())
        self.assertTrue((root / "plugins/platforms/dingtalk/plugin.yaml").is_file())

    def test_install_is_idempotent(self):
        root = self.make_target()
        first = self.installer.build_report(root)
        self.assertTrue(first["ok"], first)
        before_second = self.snapshot_core(root)
        plugin_before = {
            path.relative_to(root): path.read_bytes()
            for path in (root / "plugins/platforms/dingtalk").iterdir()
            if path.is_file()
        }

        second = self.installer.build_report(root)

        self.assertTrue(second["ok"], second)
        self.assertEqual(0, second["compat"]["changed_count"], second)
        plugin_copy = next(item for item in second["operations"] if item["name"] == "plugin.copy")
        self.assertEqual("plugin tree already matches source", plugin_copy["message"])
        self.assertEqual(before_second, self.snapshot_core(root))
        plugin_after = {
            path.relative_to(root): path.read_bytes()
            for path in (root / "plugins/platforms/dingtalk").iterdir()
            if path.is_file()
        }
        self.assertEqual(plugin_before, plugin_after)

    def test_missing_gateway_file_rolls_back_plugin_copy(self):
        root = self.make_target()
        (root / "gateway/run.py").unlink()

        report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        failures = [item["name"] for item in report["operations"] if item["status"] != "ok"]
        self.assertIn("compat.apply", failures)
        self.assertIn("rollback", {item["name"] for item in report["operations"]})
        self.assertFalse((root / "plugins/platforms/dingtalk").exists())

    def test_missing_plugin_source_stops_before_writes(self):
        root = self.make_target()
        before = self.snapshot_core(root)
        missing_source = root / "missing-source"

        with mock.patch.object(self.installer, "SOURCE_PLUGIN_DIR", missing_source):
            report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        self.assertIsNone(report["compat"])
        self.assertIsNone(report["verifier"])
        self.assertEqual(before, self.snapshot_core(root))
        self.assertFalse((root / "plugins/platforms/dingtalk").exists())

    def test_verifier_failure_rolls_back_core_and_plugin(self):
        root = self.make_target()
        before = self.snapshot_core(root)

        class FakeVerifier:
            def build_report(self, target):
                return {
                    "ok": False,
                    "target": str(target),
                    "check_count": 1,
                    "failure_count": 1,
                    "checks": [{"name": "fake", "path": ".", "status": "failed"}],
                }

        with mock.patch.object(
            self.installer,
            "_load_post_install_verifier",
            return_value=FakeVerifier(),
        ):
            report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        failures = [item["name"] for item in report["operations"] if item["status"] != "ok"]
        self.assertIn("post_install.verify", failures)
        self.assertEqual(before, self.snapshot_core(root))
        self.assertFalse((root / "plugins/platforms/dingtalk").exists())

    def test_verifier_failure_restores_existing_plugin_directory(self):
        root = self.make_target()
        old_plugin = root / "plugins/platforms/dingtalk"
        old_plugin.mkdir(parents=True)
        (old_plugin / "adapter.py").write_text("# old adapter\n", encoding="utf-8")

        class FakeVerifier:
            def build_report(self, target):
                return {
                    "ok": False,
                    "target": str(target),
                    "check_count": 1,
                    "failure_count": 1,
                    "checks": [{"name": "fake", "path": ".", "status": "failed"}],
                }

        with mock.patch.object(
            self.installer,
            "_load_post_install_verifier",
            return_value=FakeVerifier(),
        ):
            report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        self.assertEqual(["adapter.py"], sorted(path.name for path in old_plugin.iterdir()))
        self.assertEqual("# old adapter\n", (old_plugin / "adapter.py").read_text(encoding="utf-8"))

    def test_json_cli_is_machine_readable(self):
        root = self.make_target()
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--target", str(root), "--json"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"], report)
        self.assertEqual(0, report["failure_count"], report)
        self.assertEqual(EXPECTED_VERIFIER_CHECKS, report["verifier"]["check_count"], report)

    def test_dockerfile_uses_installer_for_full_plugin_tree(self):
        text = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("install_dingtalk_kit.py --target /opt/hermes", text)
        self.assertIn("overlays/hermes/plugins/platforms/dingtalk", text)
        self.assertNotIn(
            "adapter.py /opt/hermes/plugins/platforms/dingtalk/adapter.py",
            text,
        )


if __name__ == "__main__":
    unittest.main()
