"""守护 docs/BASELINE.md 的哈希表与实际文件一致。

为什么需要这个测试：BASELINE.md 的哈希表本来是"发布前核对文件没被意外改动"
的凭据，但它此前**没有任何自动化守护**——H1 治理第 6 项实测发现
`adapter.py` 的哈希早在本轮改动之前就已与实际文件不符，没人发现。
一张没人守的表等于没有，还会给人"已核对"的错觉。

同时守护 `patches/*.patch`：补丁文件与其记录的行数/哈希必须对得上，否则
"改了 overlay 但没重生成补丁"会静默漏出去。
"""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE_DOC = ROOT / "docs/BASELINE.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_rows(section_title: str) -> list[tuple[str, ...]]:
    """抽取指定小节下的表格行（去掉表头与分隔行）。"""
    text = BASELINE_DOC.read_text(encoding="utf-8")
    start = text.index(f"## {section_title}")
    rest = text[start:]
    end = rest.find("\n## ", 1)
    block = rest if end == -1 else rest[:end]
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] in ("文件",):
            continue
        rows.append(tuple(cells))
    return rows


class BaselineManifestTest(unittest.TestCase):
    def test_covered_files_hashes_match_actual(self):
        """覆盖文件表里的每个 sha256 必须与磁盘上的文件一致。

        失败时的正确处置是**重算并更新表**（改了 overlay 就该更新基线），
        而不是删掉这条测试。
        """
        rows = _parse_rows("覆盖文件")
        self.assertTrue(rows, "docs/BASELINE.md 的「覆盖文件」表为空")
        mismatched = []
        for path_cell, hash_cell in rows:
            rel = path_cell.strip("`")
            recorded = hash_cell.strip("`")
            actual_path = ROOT / rel
            if not actual_path.exists():
                mismatched.append(f"{rel}: 文件不存在")
                continue
            actual = _sha256(actual_path)
            if actual != recorded:
                mismatched.append(f"{rel}: 表内 {recorded[:16]}… 实际 {actual[:16]}…")
        self.assertEqual([], mismatched)

    def test_patch_files_hashes_and_line_counts_match(self):
        rows = _parse_rows("补丁文件")
        self.assertTrue(rows, "docs/BASELINE.md 的「补丁文件」表为空")
        mismatched = []
        for path_cell, lines_cell, hash_cell in rows:
            rel = path_cell.strip("`")
            actual_path = ROOT / rel
            if not actual_path.exists():
                mismatched.append(f"{rel}: 文件不存在")
                continue
            actual_hash = _sha256(actual_path)
            if actual_hash != hash_cell.strip("`"):
                mismatched.append(f"{rel}: sha256 不符")
            actual_lines = len(actual_path.read_text(encoding="utf-8").splitlines())
            if actual_lines != int(lines_cell):
                mismatched.append(f"{rel}: 行数 表内 {lines_cell} 实际 {actual_lines}")
        self.assertEqual([], mismatched)

    def test_every_installed_plugin_source_is_covered(self):
        """安装器会拷贝的插件 .py 源码，都应在覆盖文件表里有一行。

        防的是"新增了插件文件但忘了登记基线"——delivery_gate.py 就差点漏掉
        （H1 治理第 5 项引入、第 6 项才补登记，U36）。
        """
        recorded = {p.strip("`") for p, _ in _parse_rows("覆盖文件")}
        plugin_roots = [
            "overlays/hermes/plugins/platforms/dingtalk",
            "overlays/hermes/plugins/product_confirmation",
            # H1 治理第 6 项独立验收：此前这里漏了 h1_task_write，而
            # docs/BASELINE.md 里也一个字都没提它——测试被裁剪到刚好能过。
            "overlays/hermes/plugins/h1_task_write",
        ]
        missing = []
        for root_rel in plugin_roots:
            for src in sorted((ROOT / root_rel).glob("*.py")):
                rel = str(src.relative_to(ROOT))
                if src.name == "__init__.py":
                    continue
                if rel not in recorded:
                    missing.append(rel)
        self.assertEqual(
            [], missing,
            "以下插件源码未登记进 docs/BASELINE.md 覆盖文件表",
        )


if __name__ == "__main__":
    unittest.main()
