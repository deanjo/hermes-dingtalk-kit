"""H1 治理第 5 项 · 钉钉出站闸门（delivery_class）契约测试。

事故背景：2026-07-27 业务群里弹出英文审批卡，同批还有会话重置提示、Home
频道提示等框架内部产物。闸门按**发起来源分类**决定放行，判据是出站源头在
metadata 里显式标注的 delivery_class——不看正文、不匹配关键词。

本文件锁三件事：
1. 框架产物（审批卡/生命周期通知/心跳）不得送达；
2. 业务面（最终回复/业务确认/业务澄清）必须送达——**误杀比漏放严重得多**；
3. 未标注的一律放行（fail-open 观察期），不得因为"没见过"就吞掉用户的回复。

与 test_dingtalk_delivery_contract.py 同范式：用 ast 抽取被测函数执行，
不 import 真实 hermes 包。
"""

from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/adapter.py"
GATE_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/delivery_gate.py"
RUN_PATH = ROOT / "overlays/hermes/gateway/run.py"

_WANTED = {
    "classify_delivery",
    "DeliveryDecision",
    "DELIVERY_CLASS_KEY",
    "DELIVERABLE_CLASSES",
    "BLOCKED_CLASSES",
    "OBSERVED_CLASSES",
}


def load_gate():
    """从 delivery_gate.py 抽出闸门的模块级定义并执行。"""
    tree = ast.parse(GATE_PATH.read_text(encoding="utf-8"), filename=str(GATE_PATH))
    picked = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _WANTED:
            picked.append(copy.deepcopy(node))
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & _WANTED:
                picked.append(copy.deepcopy(node))
    missing = _WANTED - {
        getattr(n, "name", None)
        or next(t.id for t in n.targets if isinstance(t, ast.Name))
        for n in picked
    }
    if missing:
        raise RuntimeError(f"闸门定义缺失: {sorted(missing)}")

    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            ast.ImportFrom(
                module="typing",
                names=[ast.alias(name="Any"), ast.alias(name="Dict"), ast.alias(name="Optional")],
                level=0,
            ),
            *picked,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict = {}
    exec(compile(module, str(GATE_PATH), "exec"), namespace)
    return namespace


GATE = load_gate()
classify = GATE["classify_delivery"]


class BlockedClassesTest(unittest.TestCase):
    """框架产物不得进业务群。"""

    def test_approval_card_blocked(self):
        """2026-07-27 那张英文审批卡的类别。"""
        d = classify({"delivery_class": "approval_control"})
        self.assertFalse(d.allowed)
        self.assertEqual(d.delivery_class, "approval_control")

    def test_lifecycle_notice_blocked(self):
        """重启 / Home 频道提示 / 关停通知。"""
        self.assertFalse(classify({"delivery_class": "lifecycle_notice"}).allowed)

    def test_heartbeat_blocked(self):
        """⏳ Working — N min：按时延契约不算实质反馈。"""
        self.assertFalse(classify({"delivery_class": "heartbeat"}).allowed)

    def test_non_conversational_marker_blocked(self):
        """core 既有的 non_conversational 语义等价于生命周期通知。"""
        d = classify({"non_conversational": True})
        self.assertFalse(d.allowed)
        self.assertEqual(d.delivery_class, "lifecycle_notice")

    def test_blocked_even_with_reply_to(self):
        """显式标注优先于 reply_to 推断——审批卡即使带 reply_to 也不放行。"""
        self.assertFalse(
            classify({"delivery_class": "approval_control"}, reply_to="msg-1").allowed
        )


class DeliverableClassesTest(unittest.TestCase):
    """业务面必须送达——误杀最终回复 = 用户什么都收不到。"""

    def test_assistant_final_delivered(self):
        d = classify({"delivery_class": "assistant_final"}, reply_to="msg-1")
        self.assertTrue(d.allowed)
        self.assertEqual(d.delivery_class, "assistant_final")

    def test_business_confirm_delivered(self):
        """product_confirmation 直发路径，误杀即打断场景 2。"""
        d = classify(
            {"delivery_class": "business_confirm", "at_user_ids": ["staff-1"]}
        )
        self.assertTrue(d.allowed)

    def test_business_error_delivered(self):
        """引用原文缺失 / 任务绑定缺失的中文澄清。"""
        self.assertTrue(
            classify({"delivery_class": "business_error"}, reply_to="m1").allowed
        )


class FailOpenTest(unittest.TestCase):
    """未标注一律放行：观察期不得因'没见过'吞掉用户可见内容。"""

    def test_unlabeled_passthrough(self):
        d = classify({})
        self.assertTrue(d.allowed)
        self.assertEqual(d.delivery_class, "unlabeled")

    def test_none_metadata_passthrough(self):
        self.assertTrue(classify(None).allowed)

    def test_unknown_class_passthrough_but_named(self):
        """新出现的类别放行但记名，便于发现漏登记的来源。"""
        d = classify({"delivery_class": "brand_new_source"})
        self.assertTrue(d.allowed)
        self.assertEqual(d.delivery_class, "brand_new_source")
        self.assertEqual(d.reason, "declared-unknown")

    def test_observed_class_passthrough(self):
        """工具进度本轮放行：一刀切拦掉会让长任务重新变成沉默十分钟。"""
        d = classify({"delivery_class": "developer_status"})
        self.assertTrue(d.allowed)
        self.assertIn("developer_status", GATE["OBSERVED_CLASSES"])

    def test_blank_class_falls_back(self):
        self.assertTrue(classify({"delivery_class": "   "}).allowed)


class ClassSetsTest(unittest.TestCase):
    """类别集合本身的约束。"""

    def test_deliverable_and_blocked_disjoint(self):
        self.assertEqual(GATE["DELIVERABLE_CLASSES"] & GATE["BLOCKED_CLASSES"], frozenset())

    def test_observed_not_blocked(self):
        self.assertEqual(GATE["OBSERVED_CLASSES"] & GATE["BLOCKED_CLASSES"], frozenset())


class SourceLabelingTest(unittest.TestCase):
    """出站源头必须真的打了标记——闸门只认标记，源头不打就等于没治理。"""

    def setUp(self):
        self.run_src = RUN_PATH.read_text(encoding="utf-8")
        self.adapter_src = ADAPTER_PATH.read_text(encoding="utf-8")

    def test_approval_card_site_labeled(self):
        self.assertIn('_approval_metadata["delivery_class"] = "approval_control"', self.run_src)

    def test_heartbeat_site_labeled(self):
        self.assertIn('_heartbeat_metadata["delivery_class"] = "heartbeat"', self.run_src)

    def test_lifecycle_helper_covers_dingtalk(self):
        self.assertIn('if plat not in ("discord", "dingtalk")', self.run_src)
        self.assertIn('merged.setdefault("delivery_class", "lifecycle_notice")', self.run_src)

    def test_progress_labeled_as_observed_not_lifecycle(self):
        """工具进度必须被摘出生命周期类，否则会被误拦。"""
        self.assertIn('_progress_metadata["delivery_class"] = "developer_status"', self.run_src)
        self.assertIn('_progress_metadata.pop("non_conversational", None)', self.run_src)

    def test_send_calls_gate(self):
        self.assertIn("blocked_send_result(metadata, reply_to, self.name, SendResult, logger)", self.adapter_src)
        gate_src = GATE_PATH.read_text(encoding="utf-8")
        self.assertIn('"delivery_outcome": "blocked"', gate_src)

    def test_business_error_sites_labeled(self):
        binding = (ROOT / "overlays/hermes/plugins/platforms/dingtalk/task_binding.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"delivery_class": "business_error"', binding)
        # adapter 侧引用澄清走 reply_to（fail-open 放行）；task_binding 侧显式标注。

    def test_product_confirmation_labeled(self):
        tools = (ROOT / "overlays/hermes/plugins/product_confirmation/tools.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"delivery_class": "business_confirm"', tools)


if __name__ == "__main__":
    unittest.main()
