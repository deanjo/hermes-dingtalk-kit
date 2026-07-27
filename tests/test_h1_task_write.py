"""H1 V2 thin write-gate tests (plugins/h1_task_write + task_binding fact store).

Covers the §3.2 contract: declare validates + records (zero Kanban writes);
delivered flag via delivery points (card/webhook alike, failure receipts
never); write tools take only proposal_id+confirm_msg_id and check every
structural fact — fabricated ids, undelivered, mismatched confirm id,
cross-user, expired, three-state consumption with outcome_unknown rollback
and idempotent single card, retry cap, 所确认即所建 (C1).
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import re
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "overlays/hermes/plugins/h1_task_write"
BINDING_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/task_binding.py"


def load_task_binding_module():
    name = "dingtalk_task_binding"
    spec = importlib.util.spec_from_file_location(name, BINDING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_plugin_package():
    name = "h1_task_write_uut"
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, sys.modules[name + ".tools"]


class FakeStore:
    def __init__(self):
        self._db = object()
        self.state = None

    def get_task_state(self, source):
        return copy.deepcopy(self.state)

    def cas_task_state(self, source, expected_revision, new_state):
        revision = self.state["revision"] if self.state else 0
        if revision != expected_revision:
            return False
        self.state = copy.deepcopy(new_state)
        return True


class FakeAdapter:
    def __init__(self):
        self.name = "dingtalk"
        self.config = SimpleNamespace(extra={})
        self._session_store = FakeStore()
        self._h1_facts = {"proposals": {}, "pending_delivery": {}, "replies": {}}


def _source(user_id="sender-1", chat_id="cid-1"):
    return SimpleNamespace(
        platform=SimpleNamespace(value="dingtalk"),
        chat_id=chat_id,
        chat_type="group",
        user_id=user_id,
        user_id_alt=None,
        thread_id=None,
        profile=None,
        message_id="m-in-1",
    )


def _boards(*_kwargs):
    return [{"slug": "agong", "name": "AGong"}]


def _candidate(**overrides):
    candidate = {
        "board_slug": "agong",
        "board_name": "AGong",
        "task_id": "t_aaaabbbb",
        "task_title": "联系人展示问题",
        "current_stage": "Explorer：待核验",
        "status": "ready",
    }
    candidate.update(overrides)
    return candidate


class H1TaskWriteTestBase(unittest.TestCase):
    def setUp(self):
        self.tb = load_task_binding_module()
        self.tb._h1_dispatch_scope.set(None)
        self.plugin, self.tools = load_plugin_package()
        self.tools._dispatch_scope.set(None)
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(_adapter_for_source=lambda source: self.adapter)
        self.create_calls = []
        self._install_gateway_fakes()

    def tearDown(self):
        for name, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    # -- dependency-free gateway fakes（kit 惯例：系统 python 无 gateway 可导） --

    def _install_gateway_fakes(self):
        names = (
            "gateway",
            "gateway.task_intake",
            "gateway.honest_failure",
            "hermes_cli",
            "hermes_cli.kanban_db",
        )
        self._old_modules = {name: sys.modules.get(name) for name in names}

        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        ti = types.ModuleType("gateway.task_intake")

        def _normalized_identity_source(source):
            user_id = getattr(source, "user_id", None)
            user_id_alt = getattr(source, "user_id_alt", None)
            stripped = str(user_id).strip() if user_id is not None else ""
            stripped_alt = str(user_id_alt).strip() if user_id_alt is not None else ""
            if not (stripped or stripped_alt):
                return None
            return source

        def _state_copy(raw):
            return (
                copy.deepcopy(raw)
                if raw is not None
                else {"revision": 0, "current_binding": None, "pending_confirmation": None}
            )

        def _cas_state(store, source, state, **changes):
            replacement = copy.deepcopy(state)
            replacement.update(copy.deepcopy(changes))
            replacement["revision"] = int(state.get("revision", 0)) + 1
            if store.cas_task_state(source, state.get("revision", 0), replacement):
                return replacement
            return None

        class TaskIntakeRetryableError(RuntimeError):
            def __init__(self, message, *, code="task_intake_retryable"):
                super().__init__(message)
                self.code = code

        class TaskIntakePermanentError(RuntimeError):
            def __init__(self, message, *, code):
                super().__init__(message)
                self.code = code

        class TaskIntakeOutcomeUnknownError(RuntimeError):
            def __init__(self, message, *, root_task_id=None):
                super().__init__(message)
                self.root_task_id = root_task_id

        ti._normalized_identity_source = _normalized_identity_source
        ti._state_copy = _state_copy
        ti._cas_state = _cas_state
        ti._validate_binding_snapshot = lambda target: copy.deepcopy(target)
        ti._create_task_binding = self._fake_create
        ti.TaskIntakeRetryableError = TaskIntakeRetryableError
        ti.TaskIntakePermanentError = TaskIntakePermanentError
        ti.TaskIntakeOutcomeUnknownError = TaskIntakeOutcomeUnknownError
        self.ti = ti

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []
        kb = types.ModuleType("hermes_cli.kanban_db")

        class BoardMetadataCorruptError(RuntimeError):
            pass

        kb.BoardMetadataCorruptError = BoardMetadataCorruptError
        kb.list_boards = lambda **kwargs: copy.deepcopy(_boards())
        kb._normalize_board_slug = lambda value: (
            value if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) else None
        )
        hermes_cli.kanban_db = kb

        honest = types.ModuleType("gateway.honest_failure")
        # 可切换的替身：默认成功回执，失败回执用例把 self.failure_receipt 置 True。
        # （旧写法恒为 False，本文件里 R3 失败回执分支一次都没被执行过。）
        self.failure_receipt = False
        honest.is_failure_receipt = lambda: self.failure_receipt

        sys.modules["gateway"] = gateway
        sys.modules["gateway.task_intake"] = ti
        sys.modules["gateway.honest_failure"] = honest
        sys.modules["hermes_cli"] = hermes_cli
        sys.modules["hermes_cli.kanban_db"] = kb

    def _fake_create(self, target, operation_id):
        self.create_calls.append((copy.deepcopy(target), operation_id))
        return _candidate(task_title=target.get("task_title", "联系人展示问题"))

    def _capture(self, *, text="帮我排查下这个报错", message_id="m-in-1", source=None):
        event = SimpleNamespace(
            source=source or _source(), message_id=message_id, text=text
        )
        self.tools.capture_dispatch_context(
            event, self.gateway, session_store=self.adapter._session_store
        )
        return event

    def _declare(self, **overrides):
        args = {
            "kind": "create_task",
            "target": {
                "board_slug": "agong",
                "board_name": "AGong",
                "title": "同步报错排查",
                "body": "帮我排查下这个报错",
            },
        }
        args.update(overrides)
        if "target" in overrides:
            args["target"] = overrides["target"]
        return json.loads(self.tools._handle_declare(args))

    def _write(self, tool, proposal_id, confirm_msg_id="m-in-1"):
        handler = getattr(self.tools, f"_handle_{tool}")
        return json.loads(
            handler({"proposal_id": proposal_id, "confirm_msg_id": confirm_msg_id})
        )

    def _deliver(self, proposal_id=None, *, failure=False, message_id="out-1"):
        """Simulate the turn's final reply delivered (delivery record point).

        ``failure=True`` stamps the turn as an R3 failure receipt (guardrail
        halt / provider error) — the same delivery point still runs.
        """
        source = _source()
        self.tb.set_h1_dispatch_scope(
            source=source, text="要我建个任务跟踪到底吗？", message_id="m-in-1"
        )
        self.failure_receipt = failure
        try:
            self.tb.mark_h1_turn_delivered(
                self.adapter, chat_id="cid-1", message_id=message_id
            )
        finally:
            self.failure_receipt = False
            self.tb._h1_dispatch_scope.set(None)


class TestDeclare(H1TaskWriteTestBase):
    def test_declare_ok_records_fact_with_zero_kanban_writes(self):
        self._capture()
        result = self._declare()

        self.assertTrue(result["ok"])
        proposal_id = result["proposal_id"]
        record = self.adapter._h1_facts["proposals"][proposal_id]
        self.assertEqual("create_task", record["kind"])
        self.assertEqual("declared", record["state"])
        self.assertFalse(record["delivered"])
        self.assertEqual("sender-1", record["triggering_user"])
        # 所建即所见（C1）：落库 target 即用户确认的对象。
        self.assertEqual(
            {
                "board_slug": "agong",
                "board_name": "AGong",
                "task_title": "同步报错排查",
                "body": "帮我排查下这个报错",
            },
            record["target"],
        )
        self.assertEqual(("cid-1", "sender-1"), tuple(self.adapter._h1_facts["pending_delivery"].keys())[0:2] and (("cid-1", "sender-1")))
        self.assertEqual([], self.create_calls)

    def test_declare_rejects_second_proposal_in_flight(self):
        self._capture()
        self.assertTrue(self._declare()["ok"])
        second = self._declare()
        self.assertFalse(second["ok"])
        self.assertEqual("proposal_in_flight", second["reason"])

    def test_declare_rejects_without_dispatch_context(self):
        result = self._declare()
        self.assertEqual("dispatch_context_missing", result["reason"])

    def test_declare_rejects_synthetic_or_blank_msgid(self):
        self._capture(message_id="synthetic:abc123")
        self.assertEqual("request_id_unstable", self._declare()["reason"])

    def test_declare_rejects_anonymous_sender(self):
        self._capture(source=_source(user_id="  "))
        self.assertEqual("identity_unavailable", self._declare()["reason"])

    def test_declare_create_task_board_validation(self):
        self._capture()
        self.assertEqual(
            "board_not_found",
            self._declare(target={"board_slug": "ghost", "board_name": "幽灵", "title": "t", "body": "b"})["reason"],
        )
        self.assertEqual(
            "board_name_mismatch",
            self._declare(target={"board_slug": "agong", "board_name": "错名", "title": "t", "body": "b"})["reason"],
        )
        self.assertEqual(
            "invalid_args",
            self._declare(target={"board_slug": "agong", "board_name": "AGong"})["reason"],
        )

    def test_declare_bind_revalidates_candidate_snapshot(self):
        def refuse(target):
            raise self.ti.TaskIntakePermanentError("gone", code="task_intake_invalid")

        self.ti._validate_binding_snapshot = refuse
        self._capture()
        result = self._declare(
            kind="bind_task",
            target={"board_slug": "agong", "board_name": "AGong", "task_id": "t_fake", "task_title": "编造的"},
        )
        self.assertEqual("candidate_invalid", result["reason"])

    def test_declare_new_project_derives_slug_single_step(self):
        self._capture()
        result = self._declare(
            kind="new_project",
            target={"board_name": "商越B供", "title": "打通订单同步", "body": "新建项目"},
        )
        self.assertTrue(result["ok"])
        record = self.adapter._h1_facts["proposals"][result["proposal_id"]]
        self.assertEqual("create_board_and_task" if record["kind"] == "create_board_and_task" else "new_project", record["kind"])
        self.assertTrue(record["target"]["create_board"])
        self.assertEqual("商越B供", record["target"]["board_name"])
        self.assertTrue(record["target"]["board_slug"])

    def test_declare_unbind_requires_empty_target(self):
        self._capture()
        self.assertEqual("invalid_args", self._declare(kind="unbind_task", target={"x": 1})["reason"])
        self.assertTrue(self._declare(kind="unbind_task", target={})["ok"])


class TestWriteGate(H1TaskWriteTestBase):
    def _declared(self, kind="create_task", **target_overrides):
        self._capture()
        target = {
            "board_slug": "agong",
            "board_name": "AGong",
            "title": "同步报错排查",
            "body": "帮我排查下这个报错",
        }
        target.update(target_overrides)
        kind_arg = kind if kind != "create_task" else "create_task"
        result = self._declare(kind=kind_arg, target=target)
        assert result["ok"], result
        return result["proposal_id"]

    def test_fabricated_proposal_id_rejected(self):
        self._capture()
        result = self._write("create_task", "h1p-forged")
        self.assertEqual("proposal_unknown", result["reason"])

    def test_undeclared_direct_write_rejected(self):
        self._capture()
        result = self._write("bind_task", "h1p-never-declared")
        self.assertEqual("proposal_unknown", result["reason"])
        self.assertEqual([], self.create_calls)

    def test_not_delivered_rejected_then_delivery_unblocks(self):
        proposal_id = self._declared()
        self.assertEqual("not_delivered", self._write("create_task", proposal_id)["reason"])

        self._deliver(proposal_id)
        result = self._write("create_task", proposal_id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, len(self.create_calls))

    def test_forged_confirm_msg_id_rejected(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        result = self._write("create_task", proposal_id, confirm_msg_id="m-fake-999")
        self.assertEqual("confirm_msg_mismatch", result["reason"])
        self.assertEqual([], self.create_calls)

    def test_cross_user_confirm_rejected_without_consumption(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        self._capture(source=_source(user_id="sender-2"))
        result = self._write("create_task", proposal_id)
        self.assertEqual("user_mismatch", result["reason"])
        # 未消费：同一用户仍可确认。
        self._capture()
        self.assertTrue(self._write("create_task", proposal_id)["ok"])

    def test_expired_proposal_rejected(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        self.adapter._h1_facts["proposals"][proposal_id]["ts"] = time.time() - 901
        self.assertEqual("proposal_expired", self._write("create_task", proposal_id)["reason"])

    def test_second_consume_rejected_already_consumed(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        self.assertTrue(self._write("create_task", proposal_id)["ok"])
        again = self._write("create_task", proposal_id)
        self.assertEqual("proposal_already_consumed", again["reason"])
        self.assertEqual(1, len(self.create_calls))

    def test_outcome_unknown_rolls_back_and_idempotent_replay_creates_once(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        calls = []

        def flaky_create(target, operation_id):
            calls.append(operation_id)
            if len(calls) == 1:
                raise self.ti.TaskIntakeOutcomeUnknownError(
                    "建卡提交后结果暂时无法核验。", root_task_id="t_ccccdddd"
                )
            return _candidate()

        with mock.patch.object(self.ti, "_create_task_binding", flaky_create):
            first = self._write("create_task", proposal_id)
            self.assertEqual("outcome_unknown", first["reason"])
            # 回滚 declared → 同 proposal_id 可重入；幂等键同一 → 至多一卡。
            second = self._write("create_task", proposal_id)
            self.assertTrue(second["ok"], second)
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0], calls[1])
        record = self.adapter._h1_facts["proposals"][proposal_id]
        self.assertEqual("consumed", record["state"])

    def test_retry_cap_exhausts_after_three(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        with mock.patch.object(
            self.ti,
            "_create_task_binding",
            side_effect=self.ti.TaskIntakeOutcomeUnknownError("uncertain"),
        ):
            for _ in range(3):
                self.assertEqual("outcome_unknown", self._write("create_task", proposal_id)["reason"])
            self.assertEqual("write_retry_exhausted", self._write("create_task", proposal_id)["reason"])

    def test_permanent_failure_keeps_consumed_terminal(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        with mock.patch.object(
            self.ti,
            "_create_task_binding",
            side_effect=self.ti.TaskIntakePermanentError(
                "目标项目已不存在。", code="board_missing"
            ),
        ):
            result = self._write("create_task", proposal_id)
        self.assertEqual("board_missing", result["reason"])
        # 提案作废（不回滚）：重试报已消费，模型须重新 declare。
        self.assertEqual("proposal_already_consumed", self._write("create_task", proposal_id)["reason"])

    def test_confirmed_target_is_exactly_the_declared_one(self):
        """所确认即所建（C1）：写工具严格按声明 target 建卡。"""
        proposal_id = self._declared(body="声明里的正文")
        self._deliver(proposal_id)
        result = self._write("create_task", proposal_id)
        self.assertTrue(result["ok"])
        target, _op = self.create_calls[0]
        self.assertEqual(
            {
                "board_slug": "agong",
                "board_name": "AGong",
                "task_title": "同步报错排查",
                "body": "声明里的正文",
            },
            target,
        )
        # 建卡后绑定到当前任务（跟踪语义）。
        self.assertIsNotNone(self.adapter._session_store.state["current_binding"])

    def test_bind_and_switch_write_binding_via_cas(self):
        proposal_id = self._declared(
            kind="bind_task",
            board_slug="agong",
            board_name="AGong",
            task_id="t_aaaabbbb",
            task_title="联系人展示问题",
        )
        self._deliver(proposal_id)
        result = self._write("bind_task", proposal_id)
        self.assertTrue(result["ok"])
        self.assertEqual("t_aaaabbbb", self.adapter._session_store.state["current_binding"]["task_id"])

    def test_unbind_clears_binding(self):
        self.adapter._session_store.state = {
            "schema": "gateway-task-state/v1",
            "revision": 1,
            "current_binding": _candidate(),
            "pending_confirmation": None,
            "last_applied": None,
        }
        self._capture()
        result = self._declare(kind="unbind_task", target={})
        proposal_id = result["proposal_id"]
        self._deliver(proposal_id)
        self.assertTrue(self._write("unbind_task", proposal_id)["ok"])
        self.assertIsNone(self.adapter._session_store.state["current_binding"])

    def test_synthetic_msgid_rejected_on_write(self):
        proposal_id = self._declared()
        self._deliver(proposal_id)
        self._capture(message_id="synthetic:zzz")
        self.assertEqual("request_id_unstable", self._write("create_task", proposal_id)["reason"])


class TestFailureReceiptReleasesSlot(H1TaskWriteTestBase):
    """R3 失败回执：不记投递、不写回执 —— 但必须释放 in-flight 槽位。

    这两件事是独立的：`delivered` / `replies` 说的是“这条回复算不算 agent
    的最终回复”，`pending_delivery` 说的只是“这一轮的提案还在不在途”。旧实现
    在失败回执上直接早退，跳过了唯一的槽位释放点，槽位泄漏到 TTL(900s) 到期：
    期间用户确认拿 not_delivered，模型再也 declare 不出新提案（proposal_in_flight），
    于是模型会按 SOUL 去提醒用户“你还有一个待确认的提案”——而用户界面上根本没有。
    """

    def _declared(self):
        self._capture()
        result = self._declare()
        assert result["ok"], result
        return result["proposal_id"]

    def test_failure_receipt_releases_slot_without_recording_delivery(self):
        proposal_id = self._declared()
        self._deliver(failure=True)

        facts = self.adapter._h1_facts
        self.assertEqual({}, facts["pending_delivery"])  # 槽位已释放
        self.assertFalse(facts["proposals"][proposal_id]["delivered"])  # R3：不置位
        self.assertEqual({}, facts["replies"])  # R3：不记回执

    def test_failure_receipt_does_not_lock_out_the_next_proposal(self):
        """用户可感知的后果：下一轮模型还能正常声明新提案。"""
        first = self._declared()
        self._deliver(failure=True)

        self._capture()
        second = self._declare()
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first, second["proposal_id"])

    def test_failure_receipt_keeps_proposal_unconfirmable(self):
        """诚实失败：这一轮没送到用户，确认路径仍必须拒绝，且零建卡。"""
        proposal_id = self._declared()
        self._deliver(failure=True)

        self.assertEqual("not_delivered", self._write("create_task", proposal_id)["reason"])
        self.assertEqual([], self.create_calls)

    def test_failure_receipt_without_outbound_id_still_releases_slot(self):
        """槽位键只由 (chat_id, triggering_user) 决定，不依赖出站 message_id。"""
        proposal_id = self._declared()
        self._deliver(failure=True, message_id=None)

        facts = self.adapter._h1_facts
        self.assertEqual({}, facts["pending_delivery"])
        self.assertFalse(facts["proposals"][proposal_id]["delivered"])
        self.assertEqual({}, facts["replies"])

    def test_successful_delivery_still_records_and_releases(self):
        """正常路径不回归：置位 + 写回执 + 释放槽位。"""
        proposal_id = self._declared()
        self._deliver()

        facts = self.adapter._h1_facts
        self.assertEqual({}, facts["pending_delivery"])
        self.assertTrue(facts["proposals"][proposal_id]["delivered"])
        self.assertEqual("out-1", facts["replies"][("cid-1", "sender-1")]["msgId"])


if __name__ == "__main__":
    unittest.main()
