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
EXPECTED_VERIFIER_CHECKS = 51


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
            [
                "target.resolve",
                "dingtalk.source",
                "product_confirmation.source",
                "dingtalk.copy",
                "product_confirmation.copy",
                "compat.apply",
                "post_install.verify",
            ],
            [item["name"] for item in report["operations"]],
        )
        self.assertEqual(3, report["compat"]["changed_count"], report)
        self.assertEqual(EXPECTED_VERIFIER_CHECKS, report["verifier"]["check_count"], report)
        self.assertEqual(0, report["verifier"]["failure_count"], report)
        self.assertTrue((root / "plugins/platforms/dingtalk/adapter.py").is_file())
        self.assertTrue((root / "plugins/platforms/dingtalk/__init__.py").is_file())
        self.assertTrue((root / "plugins/platforms/dingtalk/plugin.yaml").is_file())
        self.assertTrue((root / "plugins/product_confirmation/__init__.py").is_file())
        self.assertTrue((root / "plugins/product_confirmation/store.py").is_file())
        self.assertTrue((root / "plugins/product_confirmation/tools.py").is_file())
        self.assertEqual(
            ["plugins/platforms/dingtalk", "plugins/product_confirmation"],
            report["owned_plugin_paths"],
        )

    def test_install_is_idempotent(self):
        root = self.make_target()
        first = self.installer.build_report(root)
        self.assertTrue(first["ok"], first)
        before_second = self.snapshot_core(root)
        plugin_before = self.snapshot_plugins(root)

        second = self.installer.build_report(root)

        self.assertTrue(second["ok"], second)
        self.assertEqual(0, second["compat"]["changed_count"], second)
        for operation_name in ("dingtalk.copy", "product_confirmation.copy"):
            plugin_copy = next(
                item for item in second["operations"] if item["name"] == operation_name
            )
            self.assertEqual("plugin tree already matches source", plugin_copy["message"])
        self.assertEqual(before_second, self.snapshot_core(root))
        self.assertEqual(plugin_before, self.snapshot_plugins(root))

    def test_missing_gateway_file_rolls_back_plugin_copy(self):
        root = self.make_target()
        (root / "gateway/run.py").unlink()

        report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        failures = [item["name"] for item in report["operations"] if item["status"] != "ok"]
        self.assertIn("compat.apply", failures)
        self.assertIn("rollback", {item["name"] for item in report["operations"]})
        self.assertFalse((root / "plugins/platforms/dingtalk").exists())
        self.assertFalse((root / "plugins/product_confirmation").exists())

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
        self.assertFalse((root / "plugins/product_confirmation").exists())

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
        self.assertFalse((root / "plugins/product_confirmation").exists())

    def test_second_plugin_copy_failure_rolls_back_first_plugin(self):
        root = self.make_target()
        before = self.snapshot_core(root)
        real_install = self.installer._install_plugin_tree

        def fail_on_product(source_dir, target_dir, temp_root, filenames,
                            operation_name, staged_name):
            if operation_name == "product_confirmation.copy":
                raise OSError("simulated product tree install failure")
            return real_install(
                source_dir,
                target_dir,
                temp_root,
                filenames,
                operation_name,
                staged_name,
            )

        with mock.patch.object(
            self.installer,
            "_install_plugin_tree",
            side_effect=fail_on_product,
        ):
            report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        self.assertIn("rollback", {item["name"] for item in report["operations"]})
        self.assertEqual(before, self.snapshot_core(root))
        self.assertFalse((root / "plugins/platforms/dingtalk").exists())
        self.assertFalse((root / "plugins/product_confirmation").exists())

    def test_new_tree_rename_failure_restores_existing_plugin_atomically(self):
        root = self.make_target()
        target = root / "plugins/platforms/dingtalk"
        target.mkdir(parents=True)
        (target / "adapter.py").write_text(
            "# existing adapter\n", encoding="utf-8"
        )
        (target / "site-local.txt").write_text(
            "must survive\n", encoding="utf-8"
        )
        before = self.snapshot_plugins(root)
        real_rename = self.installer._rename_tree
        injected = False

        def fail_new_tree_switch(source, destination):
            nonlocal injected
            if (
                not injected
                and destination == target
                and source.parent == target.parent
                and ".staged_dingtalk_plugin-" in source.name
            ):
                injected = True
                raise OSError("simulated staged-tree rename failure")
            return real_rename(source, destination)

        with tempfile.TemporaryDirectory() as temp_name:
            with mock.patch.object(
                self.installer,
                "_rename_tree",
                side_effect=fail_new_tree_switch,
            ):
                with self.assertRaisesRegex(
                    OSError, "simulated staged-tree rename failure"
                ):
                    self.installer._install_plugin_tree(
                        self.installer.SOURCE_PLUGIN_DIR,
                        target,
                        Path(temp_name),
                    )

        self.assertTrue(injected)
        self.assertEqual(before, self.snapshot_plugins(root))
        self.assertEqual(
            [],
            sorted(
                path.name
                for path in target.parent.iterdir()
                if path.name.startswith(f".{target.name}.")
            ),
        )

    def test_verifier_failure_restores_existing_plugin_directory(self):
        root = self.make_target()
        old_plugin = root / "plugins/platforms/dingtalk"
        old_plugin.mkdir(parents=True)
        (old_plugin / "adapter.py").write_text("# old adapter\n", encoding="utf-8")
        old_product = root / "plugins/product_confirmation"
        old_product.mkdir(parents=True)
        (old_product / "tools.py").write_text("# old product tools\n", encoding="utf-8")

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
        self.assertEqual(["tools.py"], sorted(path.name for path in old_product.iterdir()))
        self.assertEqual(
            "# old product tools\n",
            (old_product / "tools.py").read_text(encoding="utf-8"),
        )

    def test_missing_product_source_stops_before_writes(self):
        root = self.make_target()
        before = self.snapshot_core(root)

        with mock.patch.object(
            self.installer,
            "SOURCE_PRODUCT_PLUGIN_DIR",
            root / "missing-product-source",
        ):
            report = self.installer.build_report(root)

        self.assertFalse(report["ok"], report)
        self.assertEqual(before, self.snapshot_core(root))
        self.assertFalse((root / "plugins/platforms/dingtalk").exists())
        self.assertFalse((root / "plugins/product_confirmation").exists())

    def test_plugins_only_installs_both_trees_without_core_diff(self):
        root = self.make_target()
        before = self.snapshot_core(root)

        report = self.installer.build_report(root, plugins_only=True)

        self.assertTrue(report["ok"], report)
        self.assertEqual("plugins-only", report["installation_mode"])
        self.assertFalse(report["legacy_compat"]["requested"])
        self.assertFalse(report["legacy_compat"]["applied"])
        self.assertFalse(report["legacy_compat"]["rolled_back"])
        self.assertIsNone(report["legacy_compat"]["result"])
        self.assertEqual([], report["core_manifest"]["changed_paths"])
        self.assertEqual(before, self.snapshot_core(root))
        self.assertTrue((root / "plugins/platforms/dingtalk/adapter.py").is_file())
        self.assertTrue((root / "plugins/product_confirmation/tools.py").is_file())
        self.assertIn("compat.skip", {item["name"] for item in report["operations"]})

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
        self.assertIn("rm -rf /opt/hermes/plugins/platforms/dingtalk", text)
        self.assertIn("--plugins-only", text)
        self.assertIn("overlays/hermes/plugins/platforms/dingtalk", text)
        self.assertIn("overlays/hermes/plugins/product_confirmation", text)
        self.assertNotIn(
            "adapter.py /opt/hermes/plugins/platforms/dingtalk/adapter.py",
            text,
        )

    @staticmethod
    def snapshot_plugins(root: Path) -> dict[Path, bytes]:
        plugins_root = root / "plugins"
        if not plugins_root.exists():
            return {}
        return {
            path.relative_to(root): path.read_bytes()
            for path in plugins_root.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
