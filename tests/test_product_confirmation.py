"""Behavior tests for the Product Confirmation plugin.

The tests load the Kit-owned plugin package directly, so they exercise the
same four files copied by ``install_dingtalk_kit.py`` without requiring a
running gateway or any network access.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "overlays/hermes/plugins/product_confirmation"
OWNER = "staff-owner-001"
OTHER = "staff-other-002"
_FakePlatform = namedtuple("_FakePlatform", "value")


def load_product_package(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load product_confirmation package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, sys.modules[module_name + ".store"], sys.modules[module_name + ".tools"]


class ProductTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="product-confirmation-test-")
        self.module_name = f"product_confirmation_uut_{id(self)}"
        self.plugin, self.store_module, self.tools = load_product_package(self.module_name)
        self.db_path = Path(self.temp.name) / "product_confirmation.db"
        self.store = self.store_module.ProductConfirmationStore(self.db_path)
        self.tools._store = self.store
        self.tools._plugin_config = lambda: {
            "product_owner_staff_id": OWNER,
            "product_owner_name": "左肖肖",
        }

    def tearDown(self):
        self.store.close()
        self.tools._store = None
        for name in list(sys.modules):
            if name == self.module_name or name.startswith(self.module_name + "."):
                sys.modules.pop(name, None)
        self.temp.cleanup()

    def drafted(self, task_id="T-1", version="v1"):
        result = self.store.create_draft(
            task_id,
            version,
            "digest-1",
            source_conversation="dingtalk:conv-1",
            product_owner="左肖肖",
        )
        self.assertTrue(result["ok"], result)
        return task_id, version

    def waiting(self, task_id="T-1", version="v1", code="a1b2c3"):
        task_id, version = self.drafted(task_id, version)
        result = self.store.mark_waiting(
            task_id,
            version,
            request_evidence="dingtalk_delivery_ref=sent-42",
            confirm_code_hash=self.store_module.code_hash(code),
        )
        self.assertTrue(result["ok"], result)
        return task_id, version, code


class ProductConfirmationStoreTest(ProductTestBase):
    def test_approve_then_advance_reaches_tech_design(self):
        task_id, version, code = self.waiting()

        decided = self.store.apply_decision(
            task_id,
            version,
            "APPROVED",
            actor_id=OWNER,
            owner_id=OWNER,
            evidence="dingtalk_message_id=inbound-9",
            confirmation_code=code,
        )
        advanced = self.store.enter_tech_design(task_id)

        self.assertTrue(decided["applied"], decided)
        self.assertEqual(self.store_module.PRODUCT_APPROVED, decided["status"])
        self.assertTrue(advanced["ok"], advanced)
        self.assertEqual(self.store_module.TECH_DESIGN, self.store.get(task_id)["status"])

    def test_needs_revision_requires_a_new_immutable_version(self):
        task_id, version, code = self.waiting()
        decided = self.store.apply_decision(
            task_id,
            version,
            "NEEDS_REVISION",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code,
        )
        duplicate_version = self.store.create_draft(task_id, "v1", "changed-digest")
        redraft = self.store.create_draft(task_id, "v2", "digest-2")

        self.assertEqual(self.store_module.PRODUCT_NEEDS_REVISION, decided["status"])
        self.assertFalse(duplicate_version["ok"], duplicate_version)
        self.assertTrue(redraft["ok"], redraft)
        record = self.store.get(task_id)
        self.assertEqual(self.store_module.PRODUCT_DRAFT, record["status"])
        self.assertEqual("v2", record["proposal_version"])
        self.assertIsNone(record["decision"])
        self.assertIsNone(record["confirm_code_hash"])

    def test_non_owner_is_rejected_and_audited_without_state_change(self):
        task_id, version, code = self.waiting()

        result = self.store.apply_decision(
            task_id,
            version,
            "APPROVED",
            actor_id=OTHER,
            owner_id=OWNER,
            confirmation_code=code,
        )

        self.assertEqual("NOT_OWNER", result["reason"])
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION,
            self.store.get(task_id)["status"],
        )
        audit = self.store.decision_history(task_id)[-1]
        self.assertEqual("NOT_OWNER", audit["reason"])
        self.assertNotEqual(OTHER, audit["actor_hash"])

    def test_stale_version_is_rejected(self):
        task_id, version, code = self.waiting()
        self.store.apply_decision(
            task_id,
            version,
            "NEEDS_REVISION",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code,
        )
        self.store.create_draft(task_id, "v2", "digest-2")
        self.store.mark_waiting(
            task_id,
            "v2",
            confirm_code_hash=self.store_module.code_hash("d4e5f6"),
        )

        result = self.store.apply_decision(
            task_id,
            "v1",
            "APPROVED",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code,
        )

        self.assertEqual("STALE_VERSION", result["reason"])
        self.assertEqual("v2", result["current_version"])

    def test_duplicate_decision_is_idempotent_but_conflict_is_rejected(self):
        task_id, version, code = self.waiting()
        first = self.store.apply_decision(
            task_id,
            version,
            "APPROVED",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code,
        )
        duplicate = self.store.apply_decision(
            task_id,
            version,
            "APPROVED",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code,
        )
        conflict = self.store.apply_decision(
            task_id,
            version,
            "NEEDS_REVISION",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code,
        )

        self.assertTrue(first["applied"], first)
        self.assertTrue(duplicate["idempotent"], duplicate)
        self.assertFalse(duplicate["applied"], duplicate)
        self.assertEqual("CONFLICT", conflict["reason"])

    def test_waiting_state_and_code_gate_survive_restart(self):
        task_id, version, code = self.waiting()
        self.store.close()
        reopened = self.store_module.ProductConfirmationStore(self.db_path)
        self.store = reopened
        self.tools._store = reopened

        pending = reopened.list_pending()
        bad = reopened.apply_decision(
            task_id,
            version,
            "APPROVED",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code="wrong",
        )
        good = reopened.apply_decision(
            task_id,
            version,
            "APPROVED",
            actor_id=OWNER,
            owner_id=OWNER,
            confirmation_code=code.upper(),
        )

        self.assertEqual([task_id], [record["task_id"] for record in pending])
        self.assertEqual("BAD_CODE", bad["reason"])
        self.assertTrue(good["applied"], good)

    def test_request_claim_survives_restart_and_blocks_second_sender(self):
        task_id, version = self.drafted()
        first = self.store.claim_request(
            task_id,
            version,
            claim_id="claim-one",
            confirm_code_hash=self.store_module.code_hash("a1b2c3"),
        )
        self.store.close()
        reopened = self.store_module.ProductConfirmationStore(self.db_path)
        self.store = reopened
        self.tools._store = reopened

        second = reopened.claim_request(
            task_id,
            version,
            claim_id="claim-two",
            confirm_code_hash=self.store_module.code_hash("d4e5f6"),
        )
        status = reopened.get_request_status(task_id, version)

        self.assertTrue(first["acquired"], first)
        self.assertFalse(second["acquired"], second)
        self.assertEqual("REQUEST_IN_PROGRESS", second["reason"])
        self.assertEqual(self.store_module.REQUEST_CLAIMED, status["state"])
        self.assertEqual(1, status["attempt_count"])
        self.assertNotIn("claim", json.dumps(status))
        self.assertNotIn("a1b2c3", self.db_path.read_text(errors="ignore"))

    def test_tech_design_cannot_start_from_draft_or_waiting(self):
        task_id, version = self.drafted()
        from_draft = self.store.enter_tech_design(task_id)
        self.store.mark_waiting(task_id, version)
        from_waiting = self.store.enter_tech_design(task_id)

        self.assertFalse(from_draft["ok"], from_draft)
        self.assertFalse(from_waiting["ok"], from_waiting)
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION,
            self.store.get(task_id)["status"],
        )

    def test_old_database_schema_is_migrated_additively(self):
        self.store.close()
        old_db = Path(self.temp.name) / "old.db"
        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE confirmations (
                task_id TEXT PRIMARY KEY,
                source_conversation TEXT NOT NULL DEFAULT '',
                product_owner TEXT NOT NULL DEFAULT '',
                proposal_version TEXT NOT NULL,
                proposal_digest TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                requested_at TEXT,
                request_evidence TEXT,
                confirmed_at TEXT,
                decision TEXT,
                decision_evidence TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.close()

        migrated = self.store_module.ProductConfirmationStore(old_db)
        self.store = migrated
        self.tools._store = migrated
        migrated.create_draft("T-old", "v1", "digest")
        result = migrated.mark_waiting(
            "T-old",
            "v1",
            confirm_code_hash=self.store_module.code_hash("a1b2c3"),
        )

        self.assertTrue(result["ok"], result)
        self.assertIn("confirm_code_hash", migrated.get("T-old"))


class FakeAdapter:
    def __init__(self, success=True):
        self.success = success
        self.calls = []
        self.delivered_count = 0
        self.started = None
        self.release = None
        self.raise_error = None
        self.error = "delivery rejected"
        self.delivery_outcome = None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {"chat_id": chat_id, "content": content, "metadata": metadata}
        )
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.raise_error is not None:
            raise self.raise_error
        if self.success:
            self.delivered_count += 1
        return SimpleNamespace(
            success=self.success,
            message_id="sent-42" if self.success else "",
            error=self.error if not self.success else "",
            raw_response={
                "delivery_outcome": self.delivery_outcome or (
                    "delivered" if self.success else "rejected"
                )
            },
        )


class ProductConfirmationPublicHookTest(ProductTestBase):
    def setUp(self):
        super().setUp()
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(
            adapters={_FakePlatform("dingtalk"): self.adapter}
        )

    async def invoke_async(
        self,
        handler,
        args,
        *,
        actor=OWNER,
        chat_id="conv-1",
        platform="dingtalk",
        message_id="inbound-9",
    ):
        source = SimpleNamespace(
            platform=_FakePlatform(platform),
            chat_id=chat_id,
            user_id_alt=actor,
            message_id=message_id,
        )
        event = SimpleNamespace(source=source, message_id=message_id)
        self.tools.capture_dispatch_context(event, self.gateway)
        return json.loads(await asyncio.to_thread(handler, args))

    def invoke(self, handler, args, **context):
        return asyncio.run(self.invoke_async(handler, args, **context))

    def internal_task_id(self, task_id="T-1", chat_id="conv-1"):
        return self.store.resolve_task_key(task_id, f"dingtalk:{chat_id}")

    def stored(self, task_id="T-1", chat_id="conv-1"):
        internal = self.internal_task_id(task_id, chat_id)
        return self.store.get(internal) if internal else None

    @staticmethod
    def draft_args(task_id="T-1", version="v1"):
        return {
            "task_id": task_id,
            "proposal_version": version,
            "proposal_text": "完整产品方案",
        }

    @staticmethod
    def request_args(task_id="T-1", version="v1"):
        return {
            "task_id": task_id,
            "proposal_version": version,
            "title": "采购单导出",
            "problem": "采购员无法导出",
            "target_users": "采购员",
            "expected_behavior": "列表页新增导出按钮",
            "scope_in": ["导出按钮", "xlsx 格式"],
            "scope_out": ["权限调整"],
            "acceptance_criteria": ["导出内容与列表一致"],
        }

    def draft_and_request(self):
        drafted = self.invoke(self.tools._handle_draft, self.draft_args())
        self.assertTrue(drafted["ok"], drafted)
        requested = self.invoke(self.tools._handle_request, self.request_args())
        self.assertTrue(requested["ok"], requested)
        match = re.search(r"确认码 `([0-9a-f]{6})`", self.adapter.calls[-1]["content"])
        self.assertIsNotNone(match, self.adapter.calls[-1]["content"])
        return requested, match.group(1)

    @staticmethod
    def tech_draft_args(task_id="T-1", version="td-v1", work_kind="feature"):
        return {
            "task_id": task_id,
            "tech_design_version": version,
            "tech_design_text": "技术设计正文",
            "work_kind": work_kind,
        }

    @staticmethod
    def tech_request_args(task_id="T-1", version="td-v1"):
        return {
            "task_id": task_id,
            "tech_design_version": version,
            "title": "采购单导出",
            "design_summary": "新增只读导出服务",
            "risks": ["导出数据量"],
            "rollback_plan": "关闭入口并回滚版本",
            "verification_plan": "离线、集成与回归测试",
        }

    def enter_tech_design(self):
        _, code = self.draft_and_request()
        decided = self.invoke(
            self.tools._handle_decide,
            {
                "task_id": "T-1",
                "proposal_version": "v1",
                "decision": "APPROVED",
                "confirmation_code": code,
            },
        )
        self.assertTrue(decided["applied"], decided)
        advanced = self.invoke(
            self.tools._handle_advance,
            {"task_id": "T-1"},
        )
        self.assertTrue(advanced["ok"], advanced)

    def tech_draft_and_request(self):
        self.enter_tech_design()
        drafted = self.invoke(
            self.tools._handle_tech_design_draft,
            self.tech_draft_args(),
        )
        self.assertTrue(drafted["ok"], drafted)
        requested = self.invoke(
            self.tools._handle_tech_design_request,
            self.tech_request_args(),
        )
        self.assertTrue(requested["ok"], requested)
        match = re.search(
            r"技术设计确认码 `([0-9a-f]{6})`",
            self.adapter.calls[-1]["content"],
        )
        self.assertIsNotNone(match, self.adapter.calls[-1]["content"])
        return requested, match.group(1)

    def test_register_exposes_exactly_eight_non_coding_tools_and_public_hook(self):
        class Context:
            def __init__(self):
                self.tools = []
                self.hooks = []

            def register_tool(self, *, name, **kwargs):
                self.tools.append(name)

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

        context = Context()
        self.plugin.register(context)

        self.assertEqual(
            {
                "product_confirm_draft",
                "product_confirm_request",
                "product_confirm_decide",
                "product_confirm_advance",
                "tech_design_confirm_draft",
                "tech_design_confirm_request",
                "tech_design_confirm_decide",
                "product_confirm_status",
            },
            set(context.tools),
        )
        self.assertFalse(
            any(
                "coding" in name.lower() or "worker" in name.lower()
                for name in context.tools
            )
        )
        self.assertEqual(["pre_gateway_dispatch"], [name for name, _ in context.hooks])

    def test_bugfix_schema_requires_structured_classification_fields(self):
        bugfix_schema = self.tools.TECH_DESIGN_CONFIRM_DRAFT_SCHEMA[
            "parameters"
        ]["properties"]["bugfix_context"]

        self.assertEqual(
            {
                "environment",
                "tenant_or_scope",
                "time_window",
                "reproduction_entry",
                "risk_level",
                "change_scope",
                "core_modules",
                "deliverable",
            },
            set(bugfix_schema["required"]),
        )
        self.assertEqual(
            {
                "deep_search_or_search",
                "llm_or_prompt",
                "data_model_or_schema",
            },
            set(bugfix_schema["properties"]["core_modules"]["items"]["enum"]),
        )

    def test_request_uses_current_chat_and_structured_owner_mention(self):
        request_result, code = self.draft_and_request()

        self.assertEqual(1, len(self.adapter.calls))
        call = self.adapter.calls[0]
        self.assertEqual("conv-1", call["chat_id"])
        # H1 治理第 5 项：业务确认必须显式标 delivery_class，否则出站闸门
        # 收紧为 fail-closed 后会误杀，打断场景 2 的需求确认流程。
        self.assertEqual(
            {"at_user_ids": [OWNER], "delivery_class": "business_confirm"},
            call["metadata"],
        )
        for fragment in (
            "T-1",
            "v1",
            "采购单导出",
            "采购员无法导出",
            "范围内",
            "范围外",
            "验收标准",
            "同意进入技术设计",
            "需要修改",
        ):
            self.assertIn(fragment, call["content"])
        self.assertNotIn(code, json.dumps(request_result, ensure_ascii=False))
        self.assertNotIn(code.encode(), self.db_path.read_bytes())
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION,
            self.stored()["status"],
        )

    def test_decision_identity_comes_from_event_source_user_id_alt(self):
        _, code = self.draft_and_request()

        result = self.invoke(
            self.tools._handle_decide,
            {
                "task_id": "T-1",
                "proposal_version": "v1",
                "decision": "APPROVED",
                "confirmation_code": code,
                "actor_id": OTHER,
            },
            actor=OWNER,
            message_id="owner-reply-1",
        )

        self.assertTrue(result["applied"], result)
        record = self.stored()
        self.assertIn("owner-reply-1", record["decision_evidence"])
        self.assertNotIn(OWNER, record["decision_evidence"])

    def test_missing_public_hook_identity_fails_closed(self):
        _, code = self.draft_and_request()

        result = self.invoke(
            self.tools._handle_decide,
            {
                "task_id": "T-1",
                "proposal_version": "v1",
                "decision": "APPROVED",
                "confirmation_code": code,
            },
            actor="",
        )

        self.assertEqual("NO_ACTOR_IDENTITY", result["reason"])
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION,
            self.stored()["status"],
        )

    def test_non_owner_public_hook_identity_is_rejected(self):
        _, code = self.draft_and_request()

        result = self.invoke(
            self.tools._handle_decide,
            {
                "task_id": "T-1",
                "proposal_version": "v1",
                "decision": "APPROVED",
                "confirmation_code": code,
            },
            actor=OTHER,
        )

        self.assertEqual("NOT_OWNER", result["reason"])

    def test_wrong_conversation_is_rejected_without_cross_session_mutation(self):
        _, code = self.draft_and_request()

        result = self.invoke(
            self.tools._handle_decide,
            {
                "task_id": "T-1",
                "proposal_version": "v1",
                "decision": "APPROVED",
                "confirmation_code": code,
            },
            chat_id="conv-other",
        )

        self.assertEqual("WRONG_CONVERSATION", result["reason"])
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION,
            self.stored()["status"],
        )

    def test_delivery_failure_keeps_product_draft(self):
        self.adapter.success = False
        drafted = self.invoke(self.tools._handle_draft, self.draft_args())
        self.assertTrue(drafted["ok"], drafted)

        failed = self.invoke(self.tools._handle_request, self.request_args())
        internal = self.internal_task_id()
        failed_status = self.store.get_request_status(internal, "v1")
        self.adapter.success = True
        retried = self.invoke(self.tools._handle_request, self.request_args())
        delivered_status = self.store.get_request_status(internal, "v1")
        match = re.search(
            r"确认码 `([0-9a-f]{6})`", self.adapter.calls[-1]["content"]
        )

        self.assertIn("delivery failed", failed["error"])
        self.assertEqual("DELIVERY_FAILED", failed["reason"])
        self.assertTrue(failed["retryable"], failed)
        self.assertEqual(self.store_module.REQUEST_FAILED, failed_status["state"])
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(2, delivered_status["attempt_count"])
        self.assertEqual(self.store_module.REQUEST_DELIVERED,
                         delivered_status["state"])
        self.assertEqual(2, len(self.adapter.calls))
        self.assertEqual(1, self.adapter.delivered_count)
        self.assertIsNotNone(match)
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION,
            self.stored()["status"],
        )
        self.assertEqual(
            self.store_module.code_hash(match.group(1)),
            self.stored()["confirm_code_hash"],
        )

    def test_concurrent_request_claim_delivers_exactly_once(self):
        async def scenario():
            source = SimpleNamespace(
                platform=_FakePlatform("dingtalk"),
                chat_id="conv-1",
                user_id_alt=OWNER,
                message_id="inbound-9",
            )
            self.tools.capture_dispatch_context(
                SimpleNamespace(source=source, message_id="inbound-9"),
                self.gateway,
            )
            drafted = json.loads(
                await asyncio.to_thread(
                    self.tools._handle_draft, self.draft_args()
                )
            )
            self.assertTrue(drafted["ok"], drafted)
            self.adapter.started = asyncio.Event()
            self.adapter.release = asyncio.Event()
            first_task = asyncio.create_task(
                asyncio.to_thread(
                    self.tools._handle_request, self.request_args()
                )
            )
            await asyncio.wait_for(self.adapter.started.wait(), timeout=2)
            second = json.loads(
                await asyncio.to_thread(
                    self.tools._handle_request, self.request_args()
                )
            )
            self.adapter.release.set()
            first = json.loads(await first_task)
            return first, second

        first, second = asyncio.run(scenario())
        match = re.search(
            r"确认码 `([0-9a-f]{6})`", self.adapter.calls[0]["content"]
        )
        internal = self.internal_task_id()
        record = self.store.get(internal)
        request = self.store.get_request_status(internal, "v1")

        self.assertTrue(first["ok"], first)
        self.assertEqual(
            self.store_module.WAITING_PRODUCT_CONFIRMATION, first["status"]
        )
        self.assertFalse(second["acquired"], second)
        self.assertEqual("REQUEST_IN_PROGRESS", second["reason"])
        self.assertEqual(1, len(self.adapter.calls))
        self.assertEqual(1, self.adapter.delivered_count)
        self.assertIsNotNone(match)
        self.assertEqual(
            self.store_module.code_hash(match.group(1)),
            record["confirm_code_hash"],
        )
        self.assertEqual(self.store_module.REQUEST_DELIVERED, request["state"])
        self.assertEqual(1, request["attempt_count"])

    def test_unknown_delivery_outcome_keeps_claim_and_blocks_retry(self):
        # This is the exact SendResult shape produced by DingTalkAdapter.send
        # when its HTTP client raises ConnectionResetError.
        self.adapter.success = False
        self.adapter.error = "Connection reset by peer"
        self.adapter.delivery_outcome = "unknown"
        drafted = self.invoke(self.tools._handle_draft, self.draft_args())
        self.assertTrue(drafted["ok"], drafted)

        first = self.invoke(self.tools._handle_request, self.request_args())
        second = self.invoke(self.tools._handle_request, self.request_args())
        request = self.store.get_request_status(
            self.internal_task_id(), "v1"
        )

        self.assertEqual("REQUEST_OUTCOME_UNKNOWN", first["reason"])
        self.assertFalse(first["retryable"], first)
        self.assertEqual("REQUEST_IN_PROGRESS", second["reason"])
        self.assertEqual(self.store_module.REQUEST_CLAIMED, request["state"])
        self.assertEqual(1, len(self.adapter.calls))
        self.assertEqual(0, self.adapter.delivered_count)

    def test_missing_owner_config_refuses_untargeted_request(self):
        self.tools._plugin_config = lambda: {}
        drafted = self.invoke(self.tools._handle_draft, self.draft_args())
        self.assertTrue(drafted["ok"], drafted)

        result = self.invoke(self.tools._handle_request, self.request_args())

        self.assertIn("not configured", result["error"])
        self.assertEqual([], self.adapter.calls)

    def test_status_redacts_confirmation_code_hash(self):
        self.draft_and_request()

        single = self.invoke(
            self.tools._handle_status,
            {"task_id": "T-1"},
        )
        pending = self.invoke(self.tools._handle_status, {})

        self.assertNotIn("confirm_code_hash", json.dumps(single))
        self.assertNotIn("confirm_code_hash", json.dumps(pending))
        self.assertEqual(1, pending["count"])

    def test_draft_requires_digest_material(self):
        result = self.invoke(
            self.tools._handle_draft,
            {"task_id": "T-9", "proposal_version": "v1"},
        )

        self.assertIn("proposal_text or proposal_digest", result["error"])

    def test_same_logical_task_id_is_isolated_by_origin_conversation(self):
        first = self.invoke(
            self.tools._handle_draft,
            self.draft_args(task_id="SAME"),
            chat_id="conv-1",
        )
        second = self.invoke(
            self.tools._handle_draft,
            self.draft_args(task_id="SAME"),
            chat_id="conv-2",
        )

        first_key = self.internal_task_id("SAME", "conv-1")
        second_key = self.internal_task_id("SAME", "conv-2")
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertIsNotNone(first_key)
        self.assertIsNotNone(second_key)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual("SAME", self.store.get(first_key)["logical_task_id"])
        self.assertEqual("SAME", self.store.get(second_key)["logical_task_id"])

    def test_second_gate_approval_is_non_coding_terminal_state(self):
        _, code = self.tech_draft_and_request()

        result = self.invoke(
            self.tools._handle_tech_design_decide,
            {
                "task_id": "T-1",
                "tech_design_version": "td-v1",
                "decision": "APPROVED",
                "confirmation_code": code,
            },
        )

        self.assertTrue(result["applied"], result)
        self.assertEqual(
            self.store_module.TECH_DESIGN_APPROVED,
            self.stored()["status"],
        )
        self.assertFalse(result["coding_allowed"])
        self.assertFalse(result["worker_allowed"])
        self.assertIn("no coding or worker transition", result["next"])
        self.assertIn("不启动编码或 worker", self.adapter.calls[-1]["content"])

    def test_second_gate_identity_code_and_conversation_fail_closed(self):
        _, code = self.tech_draft_and_request()
        payload = {
            "task_id": "T-1",
            "tech_design_version": "td-v1",
            "decision": "APPROVED",
            "confirmation_code": code,
        }

        no_identity = self.invoke(
            self.tools._handle_tech_design_decide, payload, actor=""
        )
        non_owner = self.invoke(
            self.tools._handle_tech_design_decide, payload, actor=OTHER
        )
        wrong_conversation = self.invoke(
            self.tools._handle_tech_design_decide,
            payload,
            chat_id="conv-other",
        )
        bad_code = self.invoke(
            self.tools._handle_tech_design_decide,
            {**payload, "confirmation_code": "000000"},
        )

        self.assertEqual("NO_ACTOR_IDENTITY", no_identity["reason"])
        self.assertEqual("NOT_OWNER", non_owner["reason"])
        self.assertEqual("WRONG_CONVERSATION", wrong_conversation["reason"])
        self.assertEqual("BAD_CODE", bad_code["reason"])
        self.assertEqual(
            self.store_module.WAITING_TECH_DESIGN_CONFIRMATION,
            self.stored()["status"],
        )

    def test_second_gate_stale_version_and_duplicate_decision(self):
        _, code_v1 = self.tech_draft_and_request()
        revision = self.invoke(
            self.tools._handle_tech_design_decide,
            {
                "task_id": "T-1",
                "tech_design_version": "td-v1",
                "decision": "NEEDS_REVISION",
                "confirmation_code": code_v1,
            },
        )
        self.assertTrue(revision["applied"], revision)
        redraft = self.invoke(
            self.tools._handle_tech_design_draft,
            self.tech_draft_args(version="td-v2"),
        )
        self.assertTrue(redraft["ok"], redraft)
        requested = self.invoke(
            self.tools._handle_tech_design_request,
            self.tech_request_args(version="td-v2"),
        )
        self.assertTrue(requested["ok"], requested)
        code_v2 = re.search(
            r"技术设计确认码 `([0-9a-f]{6})`",
            self.adapter.calls[-1]["content"],
        ).group(1)

        stale = self.invoke(
            self.tools._handle_tech_design_decide,
            {
                "task_id": "T-1",
                "tech_design_version": "td-v1",
                "decision": "APPROVED",
                "confirmation_code": code_v1,
            },
        )
        first = self.invoke(
            self.tools._handle_tech_design_decide,
            {
                "task_id": "T-1",
                "tech_design_version": "td-v2",
                "decision": "APPROVED",
                "confirmation_code": code_v2,
            },
        )
        duplicate = self.invoke(
            self.tools._handle_tech_design_decide,
            {
                "task_id": "T-1",
                "tech_design_version": "td-v2",
                "decision": "APPROVED",
                "confirmation_code": code_v2,
            },
        )

        self.assertEqual("STALE_VERSION", stale["reason"])
        self.assertTrue(first["applied"], first)
        self.assertTrue(duplicate["idempotent"], duplicate)

    def test_second_gate_waiting_and_code_survive_restart(self):
        _, code = self.tech_draft_and_request()
        internal = self.internal_task_id()
        self.store.close()
        reopened = self.store_module.ProductConfirmationStore(self.db_path)
        self.store = reopened
        self.tools._store = reopened

        pending = reopened.list_pending_tech_design()
        result = self.invoke(
            self.tools._handle_tech_design_decide,
            {
                "task_id": "T-1",
                "tech_design_version": "td-v1",
                "decision": "APPROVED",
                "confirmation_code": code,
            },
        )

        self.assertEqual([internal], [row["task_id"] for row in pending])
        self.assertTrue(result["applied"], result)

    def test_second_gate_concurrent_request_delivers_exactly_once(self):
        self.enter_tech_design()
        drafted = self.invoke(
            self.tools._handle_tech_design_draft,
            self.tech_draft_args(),
        )
        self.assertTrue(drafted["ok"], drafted)

        async def scenario():
            source = SimpleNamespace(
                platform=_FakePlatform("dingtalk"),
                chat_id="conv-1",
                user_id_alt=OWNER,
                message_id="tech-request",
            )
            self.tools.capture_dispatch_context(
                SimpleNamespace(source=source, message_id="tech-request"),
                self.gateway,
            )
            self.adapter.started = asyncio.Event()
            self.adapter.release = asyncio.Event()
            first_task = asyncio.create_task(
                asyncio.to_thread(
                    self.tools._handle_tech_design_request,
                    self.tech_request_args(),
                )
            )
            await asyncio.wait_for(self.adapter.started.wait(), timeout=2)
            second = json.loads(
                await asyncio.to_thread(
                    self.tools._handle_tech_design_request,
                    self.tech_request_args(),
                )
            )
            self.adapter.release.set()
            first = json.loads(await first_task)
            return first, second

        prior_calls = len(self.adapter.calls)
        first, second = asyncio.run(scenario())
        self.assertTrue(first["ok"], first)
        self.assertEqual("REQUEST_IN_PROGRESS", second["reason"])
        self.assertEqual(prior_calls + 1, len(self.adapter.calls))

    def test_bugfix_gate_freezes_b0_b3_scope_and_b3_plan_only(self):
        self.enter_tech_design()
        missing = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "risk_level": "B1",
                    "change_scope": "single_surface",
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        missing_core_assessment = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "uat",
                    "tenant_or_scope": "tenant-a",
                    "time_window": "2026-07-30T10:00Z/11:00Z",
                    "reproduction_entry": "订单详情页",
                    "risk_level": "B1",
                    "change_scope": "single_surface",
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        invalid_core_assessment = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "uat",
                    "tenant_or_scope": "tenant-a",
                    "time_window": "2026-07-30T10:00Z/11:00Z",
                    "reproduction_entry": "订单详情页",
                    "risk_level": "B1",
                    "change_scope": "single_surface",
                    "core_modules": "none",
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        b0_behavioral = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "local",
                    "tenant_or_scope": "fixture",
                    "time_window": "offline-test",
                    "reproduction_entry": "unit test",
                    "risk_level": "B0",
                    "change_scope": "single_surface",
                    "core_modules": [],
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        core_b1 = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "uat",
                    "tenant_or_scope": "tenant-a",
                    "time_window": "2026-07-30T10:00Z/11:00Z",
                    "reproduction_entry": "订单详情页",
                    "risk_level": "B1",
                    "change_scope": "single_surface",
                    "core_modules": ["llm_or_prompt"],
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        b2_single_surface = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "dev",
                    "tenant_or_scope": "tenant-a",
                    "time_window": "2026-07-30T10:00Z/11:00Z",
                    "reproduction_entry": "订单详情页",
                    "risk_level": "B2",
                    "change_scope": "single_surface",
                    "core_modules": [],
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        b3_coding = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "production",
                    "tenant_or_scope": "tenant-a",
                    "time_window": "2026-07-30T10:00Z/11:00Z",
                    "reproduction_entry": "搜索入口",
                    "risk_level": "B3",
                    "change_scope": "core_module",
                    "core_modules": ["deep_search_or_search"],
                    "deliverable": "TECHNICAL_DESIGN",
                },
            },
        )
        valid = self.invoke(
            self.tools._handle_tech_design_draft,
            {
                **self.tech_draft_args(work_kind="bugfix"),
                "bugfix_context": {
                    "environment": "production",
                    "tenant_or_scope": "tenant-a",
                    "time_window": "2026-07-30T10:00Z/11:00Z",
                    "reproduction_entry": "搜索入口",
                    "risk_level": "B3",
                    "change_scope": "core_module",
                    "core_modules": ["deep_search_or_search"],
                    "deliverable": "RCA_PLAN_ONLY",
                },
            },
        )

        self.assertEqual("BUGFIX_GATE_INCOMPLETE", missing["reason"])
        self.assertEqual(
            "BUGFIX_GATE_INCOMPLETE", missing_core_assessment["reason"]
        )
        self.assertEqual(
            ["core_modules"], missing_core_assessment["missing"]
        )
        self.assertEqual(
            "INVALID_CORE_MODULES", invalid_core_assessment["reason"]
        )
        self.assertEqual("RISK_SCOPE_MISMATCH", b0_behavioral["reason"])
        self.assertEqual("CORE_MODULE_REQUIRES_B3", core_b1["reason"])
        self.assertEqual("RISK_SCOPE_MISMATCH", b2_single_surface["reason"])
        self.assertEqual("B3_PLAN_ONLY", b3_coding["reason"])
        self.assertTrue(valid["ok"], valid)
        self.assertFalse(valid["gate"]["coding_allowed"])
        self.assertFalse(valid["gate"]["worker_allowed"])

    def test_pending_status_lists_are_isolated_by_origin_conversation(self):
        def create_product_pending(logical_task_id, chat_id):
            conversation = f"dingtalk:{chat_id}"
            internal = self.tools._scoped_task_key(
                logical_task_id, conversation
            )
            created = self.store.create_draft(
                internal,
                "v1",
                "product-digest",
                source_conversation=conversation,
                product_owner="左肖肖",
                logical_task_id=logical_task_id,
            )
            self.assertTrue(created["ok"], created)
            waiting = self.store.mark_waiting(internal, "v1")
            self.assertTrue(waiting["ok"], waiting)

        def create_tech_pending(logical_task_id, chat_id):
            conversation = f"dingtalk:{chat_id}"
            internal = self.tools._scoped_task_key(
                logical_task_id, conversation
            )
            created = self.store.create_draft(
                internal,
                "v1",
                "product-digest",
                source_conversation=conversation,
                product_owner="左肖肖",
                logical_task_id=logical_task_id,
            )
            self.assertTrue(created["ok"], created)
            code = "a1b2c3"
            self.store.mark_waiting(
                internal,
                "v1",
                confirm_code_hash=self.store_module.code_hash(code),
            )
            approved = self.store.apply_decision(
                internal,
                "v1",
                "APPROVED",
                actor_id=OWNER,
                owner_id=OWNER,
                confirmation_code=code,
            )
            self.assertTrue(approved["applied"], approved)
            self.store.enter_tech_design(internal)
            tech = self.store.create_tech_design_draft(
                internal,
                "td-v1",
                "tech-digest",
                json.dumps({
                    "work_kind": "feature",
                    "coding_allowed": False,
                    "worker_allowed": False,
                }),
            )
            self.assertTrue(tech["ok"], tech)
            claim = self.store.claim_tech_design_request(
                internal,
                "td-v1",
                "claim-tech",
                self.store_module.code_hash("d4e5f6"),
            )
            self.assertTrue(claim["acquired"], claim)
            delivered = self.store.complete_tech_design_request_delivery(
                internal,
                "td-v1",
                "claim-tech",
                "sent-tech",
            )
            self.assertTrue(delivered["ok"], delivered)

        create_product_pending("P-CONV-1", "conv-1")
        create_product_pending("P-CONV-2", "conv-2")
        create_tech_pending("TD-CONV-1", "conv-1")
        create_tech_pending("TD-CONV-2", "conv-2")

        other_conversation = "dingtalk:conv-2"
        unsettled_internal = self.tools._scoped_task_key(
            "OUTBOX-CONV-2", other_conversation
        )
        self.store.create_draft(
            unsettled_internal,
            "v1",
            "digest",
            source_conversation=other_conversation,
            logical_task_id="OUTBOX-CONV-2",
        )
        self.store.claim_request(
            unsettled_internal,
            "v1",
            "claim-outbox",
            self.store_module.code_hash("f1e2d3"),
        )

        conv1 = self.invoke(
            self.tools._handle_status, {}, chat_id="conv-1"
        )
        conv2 = self.invoke(
            self.tools._handle_status, {}, chat_id="conv-2"
        )

        self.assertEqual(2, conv1["count"])
        self.assertEqual(
            ["P-CONV-1"],
            [
                row["task_id"]
                for row in conv1["waiting_product_confirmation"]
            ],
        )
        self.assertEqual(
            ["TD-CONV-1"],
            [
                row["task_id"]
                for row in conv1["waiting_tech_design_confirmation"]
            ],
        )
        self.assertEqual(0, conv1["unsettled_request_count"])
        self.assertEqual(2, conv2["count"])
        self.assertEqual(
            ["P-CONV-2"],
            [
                row["task_id"]
                for row in conv2["waiting_product_confirmation"]
            ],
        )
        self.assertEqual(
            ["TD-CONV-2"],
            [
                row["task_id"]
                for row in conv2["waiting_tech_design_confirmation"]
            ],
        )
        self.assertEqual(1, conv2["unsettled_request_count"])


if __name__ == "__main__":
    unittest.main()
