"""H1 治理第 5 项 · 钉钉出站闸门（delivery_class）。

独立成模块：adapter.py 受 1500 行源码门禁约束，且闸门本身需要能被单独测试。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# H1 治理第 5 项 · 出站闸门（delivery_class）
#
# 2026-07-27 事故：业务群里弹出英文审批卡，同批还有会话重置提示、Home 频道
# 提示等框架内部产物。切口定在 adapter 出口，判据是**发起来源分类**——由出站
# 源头在 metadata 里显式标注 delivery_class，adapter 只读标注，不猜内容、不
# 匹配关键词（关键词判据会随文案漂移，且拦不住未见过的产物）。
#
# 未标注的一律放行（fail-open），并按 developer 类记日志：拦错一条最终回复
# 造成的是"用户什么都没收到"，比漏一条框架产物严重得多。收紧为 fail-closed
# 需先有观察数据证明所有合法类都已登记，见任务卡 T5。
# ---------------------------------------------------------------------------

DELIVERY_CLASS_KEY = "delivery_class"

#: 允许送达业务群的类别。
DELIVERABLE_CLASSES = frozenset({
    "assistant_final",      # 模型最终回复
    "business_confirm",     # 业务确认（product_confirmation 等）
    "business_error",       # 业务侧澄清/错误提示（引用缺失、任务绑定缺失）
})

#: 明确不得送达业务群的类别。
BLOCKED_CLASSES = frozenset({
    "approval_control",     # 审批卡与审批控制流（2026-07-27 那张英文卡）
    "lifecycle_notice",     # 重启/Home/关停/会话重置等生命周期通知
    "heartbeat",            # ⏳ Working — N min 类心跳，按时延契约不算实质反馈
})

#: 观察类：本轮放行、只记名。``developer_status``（工具进度）承载"我正在做
#: 什么"，是时延契约里「实质反馈」的候选来源，一刀切拦掉会让长任务重新变成
#: 沉默十分钟——那正是事故的另一半。收紧前需先有观察数据。
OBSERVED_CLASSES = frozenset({
    "developer_status",
})


class DeliveryDecision:
    """出站闸门判定结果。"""

    __slots__ = ("allowed", "delivery_class", "reason")

    def __init__(self, allowed: bool, delivery_class: str, reason: str):
        self.allowed = allowed
        self.delivery_class = delivery_class
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"DeliveryDecision(allowed={self.allowed}, "
            f"class={self.delivery_class!r}, reason={self.reason!r})"
        )


def blocked_send_result(metadata, reply_to, adapter_name, result_cls, log=None):
    """闸门的 adapter 侧入口：该拦就返回一个 blocked 结果，放行则返回 None。

    收在这里而不是写进 ``adapter.send``，是因为 adapter.py 受 1500 行源码门禁
    约束（``post_install_verifier`` 的 ``plugin.line_count`` 检查）。
    """
    decision = classify_delivery(metadata, reply_to=reply_to)
    if decision.allowed:
        return None
    if log is not None:
        log.info("[%s] outbound blocked class=%s", adapter_name, decision.delivery_class)
    return result_cls(
        success=True,
        raw_response={
            "delivery_outcome": "blocked",
            "delivery_class": decision.delivery_class,
        },
    )


def classify_delivery(
    metadata: Optional[Dict[str, Any]] = None,
    *,
    reply_to: Optional[str] = None,
) -> DeliveryDecision:
    """按发起来源判定一条出站消息是否送达业务群。

    判定顺序（先显式、后推断，绝不看正文）：

    1. metadata 显式标注 ``delivery_class`` —— 唯一权威判据；
    2. ``non_conversational`` 标记 —— core 既有的生命周期语义，等价 lifecycle_notice；
    3. 未标注 —— 放行并记为 ``unlabeled``，供观察期统计。
    """
    meta = metadata or {}

    declared = meta.get(DELIVERY_CLASS_KEY)
    if isinstance(declared, str) and declared.strip():
        declared = declared.strip()
        if declared in BLOCKED_CLASSES:
            return DeliveryDecision(False, declared, "declared-blocked")
        if declared in DELIVERABLE_CLASSES:
            return DeliveryDecision(True, declared, "declared-deliverable")
        # 未知类别：放行但记名，便于发现漏登记的新来源。
        return DeliveryDecision(True, declared, "declared-unknown")

    if meta.get("non_conversational") is True:
        return DeliveryDecision(False, "lifecycle_notice", "non-conversational")

    return DeliveryDecision(True, "unlabeled", "unlabeled-passthrough")
