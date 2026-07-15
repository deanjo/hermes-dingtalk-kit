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

    def test_register_exposes_exactly_five_tools_and_public_hook(self):
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
                "product_confirm_status",
            },
            set(context.tools),
        )
        self.assertEqual(["pre_gateway_dispatch"], [name for name, _ in context.hooks])

    def test_request_uses_current_chat_and_structured_owner_mention(self):
        request_result, code = self.draft_and_request()

        self.assertEqual(1, len(self.adapter.calls))
        call = self.adapter.calls[0]
        self.assertEqual("conv-1", call["chat_id"])
        self.assertEqual({"at_user_ids": [OWNER]}, call["metadata"])
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
            self.store.get("T-1")["status"],
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
        record = self.store.get("T-1")
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
            self.store.get("T-1")["status"],
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

    def test_wrong_conversation_is_rejected_and_audited(self):
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
            "WRONG_CONVERSATION",
            self.store.decision_history("T-1")[-1]["reason"],
        )

    def test_delivery_failure_keeps_product_draft(self):
        self.adapter.success = False
        drafted = self.invoke(self.tools._handle_draft, self.draft_args())
        self.assertTrue(drafted["ok"], drafted)

        failed = self.invoke(self.tools._handle_request, self.request_args())
        failed_status = self.store.get_request_status("T-1", "v1")
        self.adapter.success = True
        retried = self.invoke(self.tools._handle_request, self.request_args())
        delivered_status = self.store.get_request_status("T-1", "v1")
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
            self.store.get("T-1")["status"],
        )
        self.assertEqual(
            self.store_module.code_hash(match.group(1)),
            self.store.get("T-1")["confirm_code_hash"],
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
        record = self.store.get("T-1")
        request = self.store.get_request_status("T-1", "v1")

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
        request = self.store.get_request_status("T-1", "v1")

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


if __name__ == "__main__":
    unittest.main()
