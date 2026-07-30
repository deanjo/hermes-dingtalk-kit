from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DINGTALK_PLUGIN_ROOT = ROOT / "overlays/hermes/plugins/platforms/dingtalk"
MAX_SOURCE_LINES = 1500
PUBLIC_PYTHON_SOURCES = [
    *sorted(DINGTALK_PLUGIN_ROOT.glob("**/*.py")),
    ROOT / "scripts/compat_patcher.py",
    ROOT / "scripts/compat_native_shapes.py",
    ROOT / "scripts/post_install_verifier.py",
    ROOT / "scripts/install_dingtalk_kit.py",
]


class DingTalkSourceSizeGateTest(unittest.TestCase):
    def test_public_dingtalk_plugin_python_sources_stay_under_1500_lines(self):
        sources = [path for path in PUBLIC_PYTHON_SOURCES if path.exists()]
        self.assertTrue(
            sources,
            f"no Python sources found under {DINGTALK_PLUGIN_ROOT.relative_to(ROOT)}",
        )

        oversized = []
        for source in sources:
            line_count = len(source.read_text(encoding="utf-8").splitlines())
            if line_count > MAX_SOURCE_LINES:
                oversized.append(
                    f"{source.relative_to(ROOT)}: {line_count} lines "
                    f"(limit {MAX_SOURCE_LINES})"
                )

        self.assertEqual([], oversized)


if __name__ == "__main__":
    unittest.main()
