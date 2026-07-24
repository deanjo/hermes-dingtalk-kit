"""Behavior tests for the H1 intake proposal seam (plugins/h1_intake_proposal
+ task_binding C1 machinery).

The tool only validates + slots; the pending installs ONLY after the turn's
non-receipt final reply is provably delivered (C1).  These tests drive the
real plugin package and the real task_binding helpers with fake gateway
modules — zero network, zero real Kanban.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "overlays/hermes/plugins/h1_intake_proposal"
BINDING_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/task_binding.py"
REPLY_CONTEXT_PATH = ROOT / "overlays/hermes/plugins/platforms/dingtalk/reply_context.py"


def load_reply_context_module():
    name = "dingtalk_reply_context_for_h1_uut"
    spec = importlib.util.spec_from_file_location(name, REPLY_CONTEXT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    sys.modules.setdefault("reply_context", module)
    return module


def load_task_binding_module():
    load_reply_context_module()
    name = "dingtalk_task_binding"
    spec = importlib.util.spec_from_file_location(name, BINDING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_plugin_package():
    name = "h1_intake_proposal_uut"
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
    def __init__(self, state=None):
        self._db = object()
        self.state = state or {
            "revision": 1,
            "current_binding": None,
            "pending_confirmation": None,
        }
        self.cas_calls = 0

    def task_state_key(self, source):
        return "sk-state"

    def get_task_state(self, source):
        return copy.deepcopy(self.state)

    def cas_task_state(self, source, expected_revision, new_state):
        self.cas_calls += 1
        if self.state.get("revision") != expected_revision:
            return False
        self.state = copy.deepcopy(new_state)
        return True


class FakeAdapter:
    def __init__(self, *, generation=7):
        self.name = "dingtalk"
        self.config = SimpleNamespace(extra={})
        self.session_key = "sk-1"
        self._active_sessions = {
            self.session_key: SimpleNamespace(_hermes_run_generation=generation)
        }
        self._session_store = FakeStore()
        self._intake_prompt_msgs = {}
        self.callbacks = []

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.callbacks.append(
            {"session_key": session_key, "callback": callback, "generation": generation}
        )


def _source():
    return SimpleNamespace(
        platform=SimpleNamespace(value="dingtalk"),
        chat_id="cid-1",
        chat_type="group",
        user_id="u1",
        user_id_alt=None,
        thread_id=None,
        profile=None,
        message_id="m-in-1",
    )


def _pending(operation_id="op-1"):
    return {
        "state": "awaiting",
        "intent": "create_task",
        "phase": "confirm_create",
        "request_id": "m-in-1",
        "operation_id": operation_id,
        "created_at": 1_000_000.0,
        "expires_at": 1_000_900.0,
        "original_text": "帮我排查下这个报错",
        "candidates": [],
        "target": {"board_slug": "agong", "board_name": "AGong"},
        "target_digest": "d" * 32,
        "prompt_body": "要我建个任务跟踪到底吗？",
    }


class H1ProposalTestBase(unittest.TestCase):
    def setUp(self):
        self.tb = load_task_binding_module()
        # ContextVar hygiene: production isolates the scope per message task;
        # tests share one context, so reset it explicitly per test.
        self.tb._h1_dispatch_scope.set(None)
        self.plugin, self.tools = load_plugin_package()
        self.tools._dispatch_scope.set(None)
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(_adapter_for_source=lambda source: self.adapter)
        self.build_calls = []
        self.install_calls = []

        old = {name: sys.modules.get(name) for name in (
            "gateway", "gateway.task_intake", "gateway.session", "gateway.honest_failure",
        )}
        self._old_modules = old

        gateway = ModuleType("gateway")
        gateway.__path__ = []
        task_intake = ModuleType("gateway.task_intake")
        task_intake.build_intake_proposal = self._fake_build
        task_intake._cas_install_prompt = self._fake_cas_install
        task_intake._state_copy = lambda raw: (
            copy.deepcopy(raw)
            if raw is not None
            else {"revision": 0, "current_binding": None, "pending_confirmation": None}
        )
        task_intake._awaiting_is_live = (
            lambda pending, now: isinstance(pending, dict)
            and pending.get("state") == "awaiting"
            and float(pending.get("expires_at") or 0) > now
        )
        session = ModuleType("gateway.session")
        session.build_session_key = (
            lambda source, **kwargs: self.adapter.session_key
        )
        honest = ModuleType("gateway.honest_failure")
        honest.is_failure_receipt = lambda: self.failure_receipt
        self.failure_receipt = False
        sys.modules["gateway"] = gateway
        sys.modules["gateway.task_intake"] = task_intake
        sys.modules["gateway.session"] = session
        sys.modules["gateway.honest_failure"] = honest

    def tearDown(self):
        self.tb._h1_dispatch_scope.set(None)
        self.tools._dispatch_scope.set(None)
        for name, module in self._old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    # -- fakes ---------------------------------------------------------------

    def _fake_build(self, session_store, source, **kwargs):
        self.build_calls.append(kwargs)
        if getattr(self, "build_result", None) is not None:
            return self.build_result
        return {
            "ok": True,
            "operation_id": "op-1",
            "pending": _pending("op-1"),
            "source": source,
        }

    def _fake_cas_install(self, session_store, source, state, pending, original_text):
        self.install_calls.append({"pending": pending, "original_text": original_text})
        new_state = copy.deepcopy(state)
        new_state["pending_confirmation"] = pending
        new_state["revision"] = int(state.get("revision", 0)) + 1
        return new_state, None

    # -- drivers -------------------------------------------------------------

    def _capture(self, *, text="帮我排查下这个报错", message_id="m-in-1"):
        event = SimpleNamespace(
            source=_source(),
            message_id=message_id,
            text=text,
        )
        self.tools.capture_dispatch_context(
            event, self.gateway, session_store=self.adapter._session_store
        )
        return event

    def _propose(self, **args):
        args.setdefault("kind", "create_task")
        args.setdefault("proposal_text", "要我建个任务跟踪到底吗？")
        args.setdefault("board_slug", "agong")
        args.setdefault("board_name", "AGong")
        args.setdefault("task_title", "同步报错排查")
        args.setdefault("body", "帮我排查下这个报错")
        return json.loads(self.tools._handle_propose(args))

    def _fire_callback(self):
        self.assertEqual(1, len(self.adapter.callbacks))
        asyncio.run(self.adapter.callbacks[0]["callback"]())


class TestH1IntakeProposeTool(H1ProposalTestBase):
    def test_propose_ok_slots_and_registers_callback(self):
        self._capture()
        result = self._propose()

        self.assertTrue(result["ok"])
        self.assertEqual("op-1", result["operation_id"])
        # I2: original_text came from the dispatch context, never tool args.
        self.assertEqual("帮我排查下这个报错", self.build_calls[0]["original_text"])
        self.assertEqual("m-in-1", self.build_calls[0]["request_id"])
        # Slot taken; callback registered against (session, generation).
        self.assertIn(self.adapter.session_key, self.adapter._h1_proposal_slots)
        callback = self.adapter.callbacks[0]
        self.assertEqual(self.adapter.session_key, callback["session_key"])
        self.assertEqual(7, callback["generation"])

    def test_dispatch_context_missing_rejects(self):
        result = self._propose()
        self.assertFalse(result["ok"])
        self.assertEqual("dispatch_context_missing", result["reason"])
        self.assertEqual([], self.adapter.callbacks)

    def test_validation_failure_passthrough_no_slot_no_callback(self):
        self._capture()
        self.build_result = {
            "ok": False,
            "reason": "pending_exists",
            "pending_prompt": "现有提案",
        }
        result = self._propose()

        self.assertFalse(result["ok"])
        self.assertEqual("pending_exists", result["reason"])
        self.assertEqual("现有提案", result["pending_prompt"])
        self.assertEqual({}, getattr(self.adapter, "_h1_proposal_slots", {}))
        self.assertEqual([], self.adapter.callbacks)

    def test_same_message_repeat_is_idempotent(self):
        self._capture()
        first = self._propose()
        second = self._propose()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(1, len(self.adapter.callbacks))

    def test_slot_busy_rejects_distinct_proposal(self):
        self._capture()
        self.assertTrue(self._propose()["ok"])
        self.build_result = {
            "ok": True,
            "operation_id": "op-2",
            "pending": _pending("op-2"),
            "source": _source(),
        }
        rejected = self._propose()

        self.assertFalse(rejected["ok"])
        self.assertEqual("proposal_in_flight", rejected["reason"])
        self.assertEqual(1, len(self.adapter.callbacks))

    def test_original_text_prefers_adapter_dispatch_scope(self):
        self._capture()
        self.tb.set_h1_dispatch_scope(
            source=_source(), text="帮我排查下这个报错（无 mention meta）", message_id="m-in-1"
        )
        result = self._propose()

        self.assertTrue(result["ok"])
        self.assertEqual(
            "帮我排查下这个报错（无 mention meta）",
            self.build_calls[0]["original_text"],
        )


class TestH1InstallAfterDelivery(H1ProposalTestBase):
    def _propose_and_capture(self):
        self._capture()
        result = self._propose()
        self.assertTrue(result["ok"])

    def _set_outcome_and_record(self, *, outcome="SUCCESS", record="out-1"):
        key = (self.adapter.session_key, 7)
        self.adapter._h1_run_outcomes = {key: SimpleNamespace(name=outcome)}
        if record:
            self.adapter._h1_final_reply_records = {key: record}

    def test_success_installs_and_registers_atomically(self):
        self._propose_and_capture()
        self._set_outcome_and_record()

        self._fire_callback()

        self.assertEqual(1, len(self.install_calls))
        self.assertEqual("op-1", self.install_calls[0]["pending"]["operation_id"])
        # Slot consumed; quote triple registered against the delivered id.
        self.assertEqual({}, self.adapter._h1_proposal_slots)
        registry = self.adapter._intake_prompt_msgs
        self.assertIn("cid-1", registry)
        operation_id, phase, digest, _expires = registry["cid-1"]["out-1"]
        self.assertEqual("op-1", operation_id)
        self.assertEqual("confirm_create", phase)
        self.assertEqual("d" * 32, digest)

    def test_failure_outcome_installs_nothing(self):
        self._propose_and_capture()
        self._set_outcome_and_record(outcome="FAILURE")

        self._fire_callback()

        self.assertEqual([], self.install_calls)
        self.assertEqual({}, self.adapter._h1_proposal_slots)  # slot dropped
        self.assertEqual({}, self.adapter._intake_prompt_msgs)

    def test_missing_delivery_record_installs_nothing(self):
        self._propose_and_capture()
        self._set_outcome_and_record(record=None)

        self._fire_callback()

        self.assertEqual([], self.install_calls)
        self.assertEqual({}, self.adapter._intake_prompt_msgs)

    def test_failure_receipt_never_records_final_reply(self):
        """R3: a sanitized provider-error receipt delivers successfully too —
        the production-point mark keeps it OUT of the delivery records, so
        the install gate stays unmet (blind-confirm hole closed)."""
        self._propose_and_capture()
        self._set_outcome_and_record(record=None)
        # Simulate: the turn's "final reply" was a stamped failure receipt.
        self.failure_receipt = True
        self.tb.record_h1_final_reply(self.adapter, "out-err")

        self.assertEqual({}, getattr(self.adapter, "_h1_final_reply_records", {}))
        self._fire_callback()
        self.assertEqual([], self.install_calls)
        self.assertEqual({}, self.adapter._intake_prompt_msgs)

    def test_state_drift_installs_nothing(self):
        self._propose_and_capture()
        self._set_outcome_and_record()
        # A live awaiting appeared between slotting and the callback.
        drift = _pending("op-other")
        drift["expires_at"] = time.time() + 900
        self.adapter._session_store.state["pending_confirmation"] = drift

        self._fire_callback()

        self.assertEqual([], self.install_calls)
        self.assertEqual({}, self.adapter._intake_prompt_msgs)

    def test_record_correlates_by_session_and_generation(self):
        """Records are generation-correlated (never 'chat latest outbound'):
        a record from generation 6 does not satisfy generation 7's gate."""
        self._propose_and_capture()
        key_old = (self.adapter.session_key, 6)
        key_new = (self.adapter.session_key, 7)
        self.adapter._h1_run_outcomes = {key_new: SimpleNamespace(name="SUCCESS")}
        self.adapter._h1_final_reply_records = {key_old: "out-stale"}

        self._fire_callback()

        self.assertEqual([], self.install_calls)
        self.assertEqual({}, self.adapter._intake_prompt_msgs)


class TestH1DeliveryRecordPoints(H1ProposalTestBase):
    def test_record_uses_dispatch_scope(self):
        source = _source()
        self.tb.set_h1_dispatch_scope(source=source, text="t", message_id="m-in-1")
        self.tb.record_h1_final_reply(self.adapter, "out-1")

        self.assertEqual(
            {(self.adapter.session_key, 7): "out-1"},
            self.adapter._h1_final_reply_records,
        )

    def test_record_without_scope_records_nothing(self):
        # Scope left unset (reset in setUp): no correlation, no record.
        self.tb.record_h1_final_reply(self.adapter, "out-x")
        self.assertEqual({}, getattr(self.adapter, "_h1_final_reply_records", {}))

    def test_capture_run_outcome_uses_event_source(self):
        event = SimpleNamespace(source=_source())
        self.tb.capture_h1_run_outcome(self.adapter, event, SimpleNamespace(name="SUCCESS"))

        self.assertEqual(
            {(self.adapter.session_key, 7): SimpleNamespace(name="SUCCESS")},
            self.adapter._h1_run_outcomes,
        )


if __name__ == "__main__":
    unittest.main()
