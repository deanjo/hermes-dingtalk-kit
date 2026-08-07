"""H1 V2 thin write gate — ``h1_declare_proposal`` + four write tools.

契约（H1_V2_BARE_AGENT_DESIGN §3.2）：

* declare 只写一行事实记录：按 kind 收结构化 target（I8 board 存在性与
  slug↔name 一致性在 declare 时校验；bind/switch 候选快照 declare 时
  ``_validate_binding_snapshot`` 重核）并返回 ``proposal_id``。无状态机、
  无 CAS、无 state.db 写入、无投递回调。
* 写工具参数仅 ``proposal_id`` + ``confirm_msg_id``（C1：业务参数一律
  从声明记录取，模型只供两个编号）。代码校验全部结构事实：提案存在
  且已投递、未过期、确认编号==当前真实入站 msgId、同一用户、三态消费
  （declared→consumed，outcome_unknown 回滚可重入，幂等键天然去重，
  重试上限 3 次）；任一不过 → 结构化 ``{ok:false, reason}``，模型说人话。
* 身份与真实 msgId 来自 ``pre_gateway_dispatch`` 捕获的平台事件本体，
  绝不取模型参数；时序统一网关本地时钟（M2）。
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib.util
import json
import logging
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PLUGIN_ID = "h1_task_write"
_WRITE_KINDS = ("create_task", "bind_task", "switch_task", "new_project", "unbind_task")


class TaskIntakeRetryableError(RuntimeError):
    def __init__(self, message: str, *, code: str = "task_intake_retryable"):
        super().__init__(message)
        self.code = code


class TaskIntakePermanentError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class TaskIntakeOutcomeUnknownError(RuntimeError):
    pass


H1_DECLARE_PROPOSAL_SCHEMA = {
    "name": "h1_declare_proposal",
    "description": (
        "Declare a task-write proposal to the user (create a task / bind an "
        "existing task / switch binding / create a project / unbind). This "
        "ONLY records one fact entry and returns a proposal_id — nothing is "
        "written to Kanban until the user confirms in a later message and a "
        "write tool is called with that proposal_id plus the real inbound "
        "message id. What you say in the same reply must match the target "
        "you declare (the user confirms against your words). Declare at "
        "most once per reply; never declare when your previous proposal is "
        "still awaiting delivery."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["create_task", "bind_task", "switch_task", "new_project", "unbind_task"],
            },
            "target": {"type": "object"},
        },
        "required": ["kind", "target"],
    },
}


def _write_schema(name: str) -> dict:
    return {
        "name": name,
        "description": (
            f"Execute the previously declared {name} proposal after the "
            "user confirmed. Pass the proposal_id returned by "
            "h1_declare_proposal and the current inbound message id as "
            "confirm_msg_id — both must be REAL (declared record and the "
            "current message number); fabricated ids are rejected and "
            "audited. Business arguments are taken from the declared "
            "record, never from your arguments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "confirm_msg_id": {"type": "string"},
            },
            "required": ["proposal_id", "confirm_msg_id"],
        },
    }


H1_WRITE_TOOL_SCHEMAS = {
    name: _write_schema(name)
    for name in ("create_task", "bind_task", "switch_task", "unbind_task")
}


@dataclass(frozen=True)
class _DispatchScope:
    """Per-message gateway context propagated into the tool worker thread."""

    source: Any = None
    message_id: str = ""
    text: str = ""
    gateway: Any = None
    session_store: Any = None


_dispatch_scope: contextvars.ContextVar[Optional[_DispatchScope]] = (
    contextvars.ContextVar("h1_task_write_dispatch_scope", default=None)
)


def capture_dispatch_context(event: Any, gateway: Any, session_store: Any = None, **kwargs: Any) -> None:
    """Capture the platform event's source OBJECT and real inbound msgId —
    never model arguments (I5 philosophy kept)."""
    source = getattr(event, "source", None)
    _dispatch_scope.set(
        _DispatchScope(
            source=source,
            message_id=str(
                getattr(event, "message_id", "")
                or getattr(source, "message_id", "")
                or ""
            ),
            text=str(getattr(event, "text", "") or ""),
            gateway=gateway,
            session_store=session_store,
        )
    )


def _check_h1_task_write_mode() -> bool:
    """Schema exposure (D4, D6-philosophy): dingtalk with the
    ``natural_task_intake`` adapter flag on — the only chat write surface."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        extra = ((cfg.get("platforms") or {}).get("dingtalk") or {}).get("extra") or {}
        return extra.get("natural_task_intake") is True
    except Exception:
        return False


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _task_binding_module():
    """Resolve the dingtalk kit's task_binding helpers (fact store).  All
    mutable state lives on the ADAPTER instance, so any loaded instance of
    the module drives the same coherent behavior."""
    for name in (
        "hermes_plugins.platforms__dingtalk.task_binding",
        "task_binding",
        "dingtalk_task_binding",
        "dingtalk_task_binding_for_intake_uut",
    ):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "declare_h1_proposal"):
            return module
    try:
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "platforms"
            / "dingtalk"
            / "task_binding.py"
        )
        spec = importlib.util.spec_from_file_location("dingtalk_task_binding", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("dingtalk_task_binding", module)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - resolve failure fails closed below
        logger.warning("h1_task_write: task_binding module unavailable", exc_info=True)
        return None


def _adapter_for(scope) -> Any:
    gateway = scope.gateway
    if gateway is None:
        return None
    try:
        return gateway._adapter_for_source(scope.source)
    except Exception:  # noqa: BLE001 - no adapter => cannot record/write
        return None


def _identity_of(source) -> str:
    user_id = str(getattr(source, "user_id", None) or "").strip()
    user_id_alt = str(getattr(source, "user_id_alt", None) or "").strip()
    return user_id or user_id_alt


def _normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


def _normalized_identity_source(source: Any) -> Optional[Any]:
    """Return a source with a non-empty, normalized sender identity."""
    user_id = str(getattr(source, "user_id", None) or "").strip()
    user_id_alt = str(getattr(source, "user_id_alt", None) or "").strip()
    if not (user_id or user_id_alt):
        return None
    if user_id == (getattr(source, "user_id", None) or "") and user_id_alt == (
        getattr(source, "user_id_alt", None) or ""
    ):
        return source
    return replace(source, user_id=user_id or None, user_id_alt=user_id_alt or None)


def _display_stage(task: Any) -> str:
    stage = str(getattr(task, "current_step_key", None) or "").strip()
    labels = {
        "explorer": "Explorer：待核验",
        "planner": "Planner：待设计",
        "implementer": "Implementer：待实现",
        "reviewer": "Reviewer：待审阅",
        "verifier": "Verifier：待验证",
        "reporter": "Reporter：待汇报",
    }
    if stage:
        return labels.get(stage.casefold(), stage)
    status = str(getattr(task, "status", None) or "未分阶段").strip()
    return f"未分阶段：{status}" if status != "未分阶段" else status


def _strict_boards() -> list[dict]:
    """Use the public board API while refusing malformed board.json files."""
    from hermes_cli import kanban_db as kb

    boards = kb.list_boards(include_archived=False)
    for board in boards:
        slug = str(board.get("slug") or board.get("board_slug") or "")
        path = kb.board_metadata_path(slug)
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TaskIntakePermanentError(
                "项目元数据损坏。", code="board_metadata_corrupt"
            ) from exc
        if not isinstance(raw, dict):
            raise TaskIntakePermanentError(
                "项目元数据损坏。", code="board_metadata_corrupt"
            )
    return boards


def _validate_binding_snapshot(target: dict) -> dict:
    """Refresh one exact task through the current public Kanban API."""
    from hermes_cli import kanban_db as kb

    board_slug = str(target.get("board_slug") or "").strip()
    task_id = str(target.get("task_id") or "").strip()
    if not board_slug or not task_id or kb._normalize_board_slug(board_slug) != board_slug:
        raise TaskIntakePermanentError(
            "任务绑定缺少内部地址。", code="binding_invalid"
        )
    try:
        boards = _strict_boards()
        board = next(
            (item for item in boards if str(item.get("slug") or "") == board_slug),
            None,
        )
        if board is None:
            raise TaskIntakePermanentError("目标项目已不存在。", code="board_missing")
        with kb.connect_closing(board=board_slug) as conn:
            task = kb.get_task(conn, task_id)
    except TaskIntakePermanentError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise TaskIntakeRetryableError(
            "当前任务暂时无法重新核验。", code="binding_revalidation_unavailable"
        ) from exc
    except Exception as exc:
        raise TaskIntakeRetryableError(
            "当前任务暂时无法重新核验。", code="binding_revalidation_unknown"
        ) from exc
    if task is None or str(task.status or "") == "archived":
        raise TaskIntakePermanentError(
            "当前任务已不存在或已归档。", code="task_missing"
        )
    board_name = str(board.get("name") or board_slug)
    return {
        "board_slug": board_slug,
        "board_name": board_name,
        "task_id": str(task.id),
        "task_title": str(task.title),
        "current_stage": _display_stage(task),
        "status": str(task.status),
    }


def _create_task_binding(target: dict, operation_id: str) -> dict:
    """Create one confirmed intake card through the current public Kanban API."""
    from hermes_cli import kanban_db as kb

    board_slug = str(target["board_slug"])
    board_name = str(target["board_name"])
    idempotency_key = f"h1-task-write:{operation_id}"
    try:
        if target.get("create_board"):
            if kb.board_exists(board_slug):
                error = _validate_create_target(
                    {"board_slug": board_slug, "board_name": board_name}
                )
                if error is not None:
                    raise TaskIntakePermanentError(
                        "项目名称与已有项目冲突。", code=str(error["reason"])
                    )
            else:
                kb.create_board(board_slug, name=board_name)
        else:
            error = _validate_create_target(
                {"board_slug": board_slug, "board_name": board_name}
            )
            if error is not None:
                raise TaskIntakePermanentError(
                    "目标项目不可用。", code=str(error["reason"])
                )
        with kb.connect_closing(board=board_slug) as conn:
            task_id = kb.create_task(
                conn,
                title=str(target["task_title"]),
                body=str(target.get("body") or ""),
                created_by="h1-task-write",
                idempotency_key=idempotency_key,
                triage=True,
                board=board_slug,
            )
    except TaskIntakePermanentError:
        raise
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise TaskIntakeRetryableError(
            "任务卡暂时无法创建。", code="task_create_unavailable"
        ) from exc
    try:
        return _validate_binding_snapshot(
            {"board_slug": board_slug, "task_id": str(task_id)}
        )
    except Exception as exc:
        raise TaskIntakeOutcomeUnknownError("建卡结果暂时无法核验。") from exc


def _project_slug(display_name: str) -> str:
    """Derive the board slug for a new project (code derivation, same
    algorithm the V1 intake used; never model-supplied)."""
    from hermes_cli import kanban_db as kb

    normalized = _normalize_text(display_name)
    if normalized and normalized.isascii():
        slug = re.sub(r"[^a-z0-9_-]+", "-", normalized.lower()).strip("-_")
        candidate = slug[:64].rstrip("-_") if slug else ""
    else:
        candidate = ""
    if not candidate:
        candidate = f"p-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]}"
    normalized_slug = kb._normalize_board_slug(candidate)
    if normalized_slug is None:
        raise ValueError("project name produced an empty board slug")
    return normalized_slug


def _required_fields(target: Any, fields: tuple[str, ...]) -> Optional[str]:
    if not isinstance(target, dict):
        return "target"
    missing = [
        name for name in fields
        if not (isinstance(target.get(name), str) and target.get(name).strip())
    ]
    return ",".join(missing) if missing else None


def _validate_create_target(target: dict) -> dict | None:
    """Validate board existence and slug/name consistency, failing closed."""
    try:
        boards = _strict_boards()
    except TaskIntakePermanentError as exc:
        return {"ok": False, "reason": exc.code}
    except Exception:
        return {"ok": False, "reason": "boards_unavailable"}
    wanted_slug = str(target["board_slug"]).strip()
    wanted_name = _normalize_text(target["board_name"])
    matched = None
    slug_seen = False
    for board in boards:
        slug = str(board.get("slug") or board.get("board_slug") or "")
        if slug == wanted_slug:
            slug_seen = True
            name = str(board.get("name") or board.get("display_name") or slug)
            if _normalize_text(name) == wanted_name:
                matched = {"board_slug": slug, "board_name": name}
            break
    if matched is None:
        reason = "board_name_mismatch" if slug_seen else "board_not_found"
        return {"ok": False, "reason": reason}
    target["board_slug"] = matched["board_slug"]
    target["board_name"] = matched["board_name"]
    return None


def _validate_declared_target(kind: str, target: Any) -> dict | None:
    """Per-kind structural validation at declare time; returns error or None."""
    if kind == "create_task":
        missing = _required_fields(target, ("board_slug", "board_name", "title", "body"))
        if missing:
            return {"ok": False, "reason": "invalid_args", "detail": missing}
        error = _validate_create_target(target)
        if error is not None:
            return error
        target["task_title"] = target.pop("title")
        return None
    if kind in ("bind_task", "switch_task"):
        missing = _required_fields(target, ("board_slug", "board_name", "task_id", "task_title"))
        if missing:
            return {"ok": False, "reason": "invalid_args", "detail": missing}
        try:
            snapshot = _validate_binding_snapshot(target)
        except TaskIntakePermanentError as exc:
            return {"ok": False, "reason": "candidate_invalid", "detail": str(exc)}
        except (TaskIntakeRetryableError, Exception) as exc:
            return {"ok": False, "reason": "candidate_unavailable", "detail": str(exc)}
        target.clear()
        target.update(snapshot)
        return None
    if kind == "new_project":
        missing = _required_fields(target, ("board_name", "title", "body"))
        if missing:
            return {"ok": False, "reason": "invalid_args", "detail": missing}
        try:
            slug = _project_slug(target["board_name"])
        except ValueError:
            return {"ok": False, "reason": "invalid_args", "detail": "board_name"}
        target["board_slug"] = slug
        target["board_name"] = _normalize_text(target["board_name"])
        target["create_board"] = True
        target["task_title"] = target.pop("title")
        return None
    if kind == "unbind_task":
        if target not in ({}, None):
            return {"ok": False, "reason": "invalid_args", "detail": "target"}
        return None
    return {"ok": False, "reason": "invalid_kind"}


def _handle_declare(args: dict, **_kwargs: Any) -> str:
    """Write one fact record (§3.2 declare half). Zero Kanban writes."""
    scope = _dispatch_scope.get()
    if scope is None or scope.source is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})
    tb = _task_binding_module()
    adapter = _adapter_for(scope)
    if tb is None or adapter is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})
    # msgId fail-closed: the declaration is bound to a real inbound id.
    request_id = scope.message_id.strip()
    if not request_id or request_id.casefold().startswith("synthetic:"):
        return _json({"ok": False, "reason": "request_id_unstable"})
    identity_source = _normalized_identity_source(scope.source)
    if identity_source is None:
        return _json({"ok": False, "reason": "identity_unavailable"})
    triggering_user = _identity_of(identity_source)
    chat_id = str(getattr(identity_source, "chat_id", "") or "")
    kind = str(args.get("kind") or "")
    if kind not in _WRITE_KINDS:
        return _json({"ok": False, "reason": "invalid_kind"})
    target = dict(args.get("target") or {})
    error = _validate_declared_target(kind, target)
    if error is not None:
        return _json(error)

    record = {
        "proposal_id": f"h1p-{uuid.uuid4().hex[:16]}",
        "kind": kind,
        "target": target,
        "triggering_user": triggering_user,
        "chat_id": chat_id,
        "ts": time.time(),
        "delivered": False,
        "state": "declared",
        "retries": 0,
    }
    status = tb.declare_h1_proposal(adapter, record)
    if status is not None:
        return _json({"ok": False, "reason": status})
    return _json({"ok": True, "proposal_id": record["proposal_id"]})


def _write_binding(scope, source, binding) -> None:
    """Persist the current binding in Core's existing session metadata."""
    tb = _task_binding_module()
    adapter = _adapter_for(scope)
    if tb is None or adapter is None or not tb.set_h1_binding(
        adapter,
        source,
        binding,
        session_store=scope.session_store,
    ):
        raise TaskIntakeRetryableError(
            "任务绑定状态暂时无法保存。", code="binding_state_unavailable"
        )


def _apply_write(kind: str, record: dict, scope, source) -> dict:
    """Business write OUTSIDE the consumption lock (idempotency key from the
    proposal id — a committed bootstrap deduplicates replays naturally)."""
    target = record["target"]
    if kind in ("create_task", "new_project"):
        operation_id = hashlib.sha256(
            f"h1-v2:{record['proposal_id']}".encode("utf-8")
        ).hexdigest()[:32]
        binding = _create_task_binding(target, operation_id)
        # 建卡即绑定（H1「跟踪到底」语义）——用户确认后的首轮带看板上下文。
        _write_binding(scope, source, binding)
        return {"binding": binding}
    if kind in ("bind_task", "switch_task"):
        binding = _validate_binding_snapshot(target)
        _write_binding(scope, source, binding)
        return {"binding": binding}
    _write_binding(scope, source, None)
    return {"binding": None}


def _handle_write(kind: str, args: dict, **_kwargs: Any) -> str:
    """§3.2 write gate: five structural fact checks + three-state
    consumption + idempotent write + one audit line per attempt."""
    scope = _dispatch_scope.get()
    if scope is None or scope.source is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})
    tb = _task_binding_module()
    adapter = _adapter_for(scope)
    if tb is None or adapter is None or scope.session_store is None:
        return _json({"ok": False, "reason": "dispatch_context_missing"})
    proposal_id = str(args.get("proposal_id") or "").strip()
    confirm_msg_id = str(args.get("confirm_msg_id") or "").strip()
    if not proposal_id or not confirm_msg_id:
        return _json({"ok": False, "reason": "invalid_args"})
    request_id = scope.message_id.strip()
    if not request_id or request_id.casefold().startswith("synthetic:"):
        return _json({"ok": False, "reason": "request_id_unstable"})
    # ③ confirm_msg_id must be THIS turn's real inbound msgId (fabrication-proof).
    if confirm_msg_id != request_id:
        return _json({"ok": False, "reason": "confirm_msg_mismatch"})
    identity_source = _normalized_identity_source(scope.source)
    if identity_source is None:
        return _json({"ok": False, "reason": "identity_unavailable"})
    # ④ the confirming user must be the declaring user (group-chat safe).
    record = tb.get_h1_proposal(adapter, proposal_id)
    if record is None:
        return _json({"ok": False, "reason": "proposal_unknown"})
    if record.get("triggering_user") != _identity_of(identity_source):
        return _json({"ok": False, "reason": "user_mismatch"})
    # ①②⑤ existence + delivered + unexpired + three-state consume (locked flip).
    reason = tb.try_consume_h1_proposal(adapter, proposal_id)
    if reason is not None:
        return _json({"ok": False, "reason": reason})
    # ⑥ idempotent write outside the lock.
    try:
        outcome = _apply_write(record["kind"], record, scope, identity_source)
    except TaskIntakeOutcomeUnknownError:
        # Result uncertain: rollback to declared — the idempotency key makes
        # a same-proposal replay finish exactly once (I2).
        tb.rollback_h1_consumption(adapter, proposal_id)
        _audit(scope, proposal_id, confirm_msg_id, kind, record["target"], "outcome_unknown")
        return _json({"ok": False, "reason": "outcome_unknown"})
    except TaskIntakePermanentError as exc:
        # The declared target itself is invalid now — the proposal is void
        # (consumed stays terminal); the model must re-declare.
        _audit(scope, proposal_id, confirm_msg_id, kind, record["target"], f"rejected:{exc.code}")
        return _json({"ok": False, "reason": getattr(exc, "code", "write_rejected"), "detail": str(exc)})
    except Exception:
        tb.rollback_h1_consumption(adapter, proposal_id)
        _audit(scope, proposal_id, confirm_msg_id, kind, record["target"], "outcome_unknown")
        return _json({"ok": False, "reason": "outcome_unknown"})
    _audit(scope, proposal_id, confirm_msg_id, kind, record["target"], "ok")
    return _json({"ok": True, "kind": record["kind"], "target": record["target"], "binding": outcome.get("binding")})


def _audit(scope, proposal_id: str, confirm_msg_id: str, kind: str, target: Any, outcome: str) -> None:
    logger.info(
        "h1 task write audit: actor=%s proposal_id=%s confirm_msg_id=%s kind=%s target=%s outcome=%s",
        _identity_of(scope.source),
        proposal_id,
        confirm_msg_id,
        kind,
        json.dumps(target, ensure_ascii=False, sort_keys=True),
        outcome,
    )


def _handle_create_task(args: dict, **kwargs: Any) -> str:
    return _handle_write("create_task", args, **kwargs)


def _handle_bind_task(args: dict, **kwargs: Any) -> str:
    return _handle_write("bind_task", args, **kwargs)


def _handle_switch_task(args: dict, **kwargs: Any) -> str:
    return _handle_write("switch_task", args, **kwargs)


def _handle_unbind_task(args: dict, **kwargs: Any) -> str:
    return _handle_write("unbind_task", args, **kwargs)
