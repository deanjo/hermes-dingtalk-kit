"""Durable product-confirmation state machine backed by SQLite.

Implements the contract in
``docs/hermes1-agong-dev-loop_20260707/profile-evaluation/02_product_confirmation_contract.md``:

    PRODUCT_DRAFT
      -> WAITING_PRODUCT_CONFIRMATION
          -> PRODUCT_NEEDS_REVISION -> PRODUCT_DRAFT
          -> PRODUCT_APPROVED -> TECH_DESIGN
              -> WAITING_TECH_DESIGN_CONFIRMATION
                  -> TECH_DESIGN_NEEDS_REVISION -> TECH_DESIGN
                  -> TECH_DESIGN_APPROVED

Design constraints (all enforced here, not in prompts/SOUL):

* Every transition is a conditional ``UPDATE ... WHERE status = ?`` guarded by
  ``rowcount`` (the ``handoff_state`` pattern in ``hermes_state.py``), so a
  concurrent or repeated call can never double-apply a decision.
* Only the configured product owner's platform identity may decide, and only
  for the record's *current* proposal version — a stale-version reply or a
  non-owner reply is logged and rejected without touching state.
* Repeated identical decisions are idempotent; conflicting decisions after a
  terminal state are rejected.
* State lives in its own DB file under ``HERMES_HOME`` (``/opt/data`` in the
  container volume), so a gateway restart recovers every waiting confirmation
  via :meth:`ProductConfirmationStore.list_pending`.
* The DB stores only low-sensitivity references: the decision log keeps a
  hash prefix of the actor id, never nicknames or message bodies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# -- states -----------------------------------------------------------------

PRODUCT_DRAFT = "PRODUCT_DRAFT"
WAITING_PRODUCT_CONFIRMATION = "WAITING_PRODUCT_CONFIRMATION"
PRODUCT_NEEDS_REVISION = "PRODUCT_NEEDS_REVISION"
PRODUCT_APPROVED = "PRODUCT_APPROVED"
TECH_DESIGN = "TECH_DESIGN"
WAITING_TECH_DESIGN_CONFIRMATION = "WAITING_TECH_DESIGN_CONFIRMATION"
TECH_DESIGN_NEEDS_REVISION = "TECH_DESIGN_NEEDS_REVISION"
TECH_DESIGN_APPROVED = "TECH_DESIGN_APPROVED"

# Persistent request-outbox states.  They are deliberately separate from the
# product workflow states above: CLAIMED means one caller owns the right to
# send, not that the product owner has received the request.
REQUEST_CLAIMED = "CLAIMED"
REQUEST_DELIVERED = "DELIVERED"
REQUEST_FAILED = "FAILED"

ALL_STATES = (
    PRODUCT_DRAFT,
    WAITING_PRODUCT_CONFIRMATION,
    PRODUCT_NEEDS_REVISION,
    PRODUCT_APPROVED,
    TECH_DESIGN,
    WAITING_TECH_DESIGN_CONFIRMATION,
    TECH_DESIGN_NEEDS_REVISION,
    TECH_DESIGN_APPROVED,
)

# -- decisions ---------------------------------------------------------------

DECISION_APPROVED = "APPROVED"
DECISION_NEEDS_REVISION = "NEEDS_REVISION"

_DECISION_ALIASES = {
    "approved": DECISION_APPROVED,
    "approve": DECISION_APPROVED,
    "needs_revision": DECISION_NEEDS_REVISION,
    "revise": DECISION_NEEDS_REVISION,
}

_DECISION_TO_STATE = {
    DECISION_APPROVED: PRODUCT_APPROVED,
    DECISION_NEEDS_REVISION: PRODUCT_NEEDS_REVISION,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmations (
    task_id             TEXT PRIMARY KEY,
    logical_task_id     TEXT NOT NULL DEFAULT '',
    source_conversation TEXT NOT NULL DEFAULT '',
    product_owner       TEXT NOT NULL DEFAULT '',
    proposal_version    TEXT NOT NULL,
    proposal_digest     TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    requested_at        TEXT,
    request_evidence    TEXT,
    confirm_code_hash   TEXT,
    confirmed_at        TEXT,
    decision            TEXT,
    decision_evidence   TEXT,
    tech_design_version TEXT,
    tech_design_digest  TEXT,
    tech_design_gate_json TEXT,
    tech_requested_at   TEXT,
    tech_request_evidence TEXT,
    tech_confirm_code_hash TEXT,
    tech_confirmed_at   TEXT,
    tech_decision       TEXT,
    tech_decision_evidence TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmation_versions (
    task_id          TEXT NOT NULL,
    proposal_version TEXT NOT NULL,
    proposal_digest  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    PRIMARY KEY (task_id, proposal_version)
);

CREATE TABLE IF NOT EXISTS decision_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL,
    proposal_version TEXT NOT NULL,
    actor_hash       TEXT NOT NULL,
    decision         TEXT NOT NULL,
    accepted         INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tech_design_versions (
    task_id             TEXT NOT NULL,
    tech_design_version TEXT NOT NULL,
    tech_design_digest  TEXT NOT NULL,
    tech_design_gate_json TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (task_id, tech_design_version)
);

CREATE TABLE IF NOT EXISTS confirmation_requests (
    task_id             TEXT NOT NULL,
    proposal_version    TEXT NOT NULL,
    state               TEXT NOT NULL,
    claim_id            TEXT NOT NULL,
    confirm_code_hash   TEXT NOT NULL,
    delivery_ref        TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 1,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (task_id, proposal_version)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_status ON confirmations(status);
CREATE INDEX IF NOT EXISTS idx_decision_log_task ON decision_log(task_id);
CREATE INDEX IF NOT EXISTS idx_confirmation_requests_state
    ON confirmation_requests(state);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _actor_hash(actor_id: str) -> str:
    """Low-sensitivity reference to an actor: sha256 prefix, never the raw id."""
    if not actor_id:
        return "unknown"
    return hashlib.sha256(actor_id.encode("utf-8")).hexdigest()[:12]


def code_hash(confirmation_code: str) -> str:
    """Hash of a one-time confirmation code; only the hash is ever stored."""
    return hashlib.sha256(confirmation_code.encode("utf-8")).hexdigest()


def normalize_decision(decision: str) -> Optional[str]:
    """Map user-facing decision spellings to canonical values, else None."""
    if not decision:
        return None
    return _DECISION_ALIASES.get(str(decision).strip().lower())


def default_db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "product_confirmation.db"


class ProductConfirmationStore:
    """SQLite-backed store; one instance may be shared across tool threads."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=5.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(self._conn, db_label="product_confirmation.db")
        except Exception:
            # WAL is an optimization; DELETE journal mode is still correct.
            logger.debug("WAL setup failed for product_confirmation.db", exc_info=True)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # Additive migration guard: CREATE TABLE IF NOT EXISTS never alters an
        # existing table, so columns added after the first release must be
        # backfilled here or a pre-existing DB file fails every write.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(confirmations)")}
        additive_columns = {
            "logical_task_id": "TEXT NOT NULL DEFAULT ''",
            "confirm_code_hash": "TEXT",
            "tech_design_version": "TEXT",
            "tech_design_digest": "TEXT",
            "tech_design_gate_json": "TEXT",
            "tech_requested_at": "TEXT",
            "tech_request_evidence": "TEXT",
            "tech_confirm_code_hash": "TEXT",
            "tech_confirmed_at": "TEXT",
            "tech_decision": "TEXT",
            "tech_decision_evidence": "TEXT",
        }
        for column, declaration in additive_columns.items():
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE confirmations ADD COLUMN {column} {declaration}"
                )
        self._conn.execute(
            "UPDATE confirmations SET logical_task_id = task_id"
            " WHERE logical_task_id IS NULL OR logical_task_id = ''"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS"
            " idx_confirmations_conversation_logical"
            " ON confirmations(source_conversation, logical_task_id)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- helpers -------------------------------------------------------------

    def _row(self, task_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM confirmations WHERE task_id = ?", (task_id,)
        )
        return cur.fetchone()

    @staticmethod
    def _tech_request_version(tech_design_version: str) -> str:
        return f"tech:{tech_design_version}"

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {k: row[k] for k in row.keys()}

    def _rollback_quietly(self) -> None:
        """Roll back without masking the original exception (disk full, lock)."""
        try:
            self._conn.execute("ROLLBACK")
        except Exception:
            logger.debug("rollback failed after write error", exc_info=True)

    def _log_decision(
        self, task_id: str, version: str, actor_id: str,
        decision: str, accepted: bool, reason: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO decision_log (task_id, proposal_version, actor_hash,"
            " decision, accepted, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, version, _actor_hash(actor_id), decision,
             1 if accepted else 0, reason, _now()),
        )

    # -- transitions ----------------------------------------------------------

    def create_draft(
        self,
        task_id: str,
        proposal_version: str,
        proposal_digest: str,
        source_conversation: str = "",
        product_owner: str = "",
        logical_task_id: str = "",
    ) -> Dict[str, Any]:
        """Create a new task in PRODUCT_DRAFT, or re-draft after NEEDS_REVISION.

        Each (task_id, proposal_version) pair is registered exactly once so a
        version uniquely locates one proposal. Re-drafting is only legal from
        PRODUCT_DRAFT / PRODUCT_NEEDS_REVISION — an in-flight confirmation
        (WAITING) or an approved task cannot be silently replaced.
        """
        if not task_id or not proposal_version:
            return {"ok": False, "error": "task_id and proposal_version are required"}
        logical_task_id = logical_task_id or task_id
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(task_id)
                dup = self._conn.execute(
                    "SELECT 1 FROM confirmation_versions WHERE task_id = ?"
                    " AND proposal_version = ?",
                    (task_id, proposal_version),
                ).fetchone()
                if dup:
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "error": f"proposal_version {proposal_version!r} already"
                                 f" registered for task {task_id!r}; versions are"
                                 " immutable — bump the version instead",
                    }
                if row is not None and row["status"] == PRODUCT_DRAFT:
                    in_flight = self._conn.execute(
                        "SELECT 1 FROM confirmation_requests WHERE task_id = ?"
                        " AND proposal_version = ? AND state = ?",
                        (task_id, row["proposal_version"], REQUEST_CLAIMED),
                    ).fetchone()
                    if in_flight:
                        self._conn.execute("ROLLBACK")
                        return {
                            "ok": False,
                            "reason": "REQUEST_IN_PROGRESS",
                            "status": PRODUCT_DRAFT,
                            "current_version": row["proposal_version"],
                            "error": "a confirmation request send is already in"
                                     " progress for the current version; the"
                                     " draft cannot be replaced until delivery"
                                     " is reconciled",
                        }
                if row is None:
                    self._conn.execute(
                        "INSERT INTO confirmations (task_id, logical_task_id,"
                        " source_conversation,"
                        " product_owner, proposal_version, proposal_digest, status,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (task_id, logical_task_id, source_conversation, product_owner,
                         proposal_version, proposal_digest, PRODUCT_DRAFT, now, now),
                    )
                elif row["status"] in (PRODUCT_DRAFT, PRODUCT_NEEDS_REVISION):
                    self._conn.execute(
                        "UPDATE confirmations SET proposal_version = ?,"
                        " proposal_digest = ?, status = ?, requested_at = NULL,"
                        " request_evidence = NULL, confirm_code_hash = NULL,"
                        " confirmed_at = NULL,"
                        " decision = NULL, decision_evidence = NULL, updated_at = ?"
                        " WHERE task_id = ? AND status IN (?, ?)",
                        (proposal_version, proposal_digest, PRODUCT_DRAFT, now,
                         task_id, PRODUCT_DRAFT, PRODUCT_NEEDS_REVISION),
                    )
                else:
                    status = row["status"]
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "error": f"task {task_id!r} is in {status}; a new draft is"
                                 " only allowed from PRODUCT_DRAFT or"
                                 " PRODUCT_NEEDS_REVISION",
                        "status": status,
                    }
                self._conn.execute(
                    "INSERT INTO confirmation_versions (task_id, proposal_version,"
                    " proposal_digest, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, proposal_version, proposal_digest, now),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {"ok": True, "task_id": task_id, "status": PRODUCT_DRAFT,
                "proposal_version": proposal_version}

    def claim_request(
        self,
        task_id: str,
        proposal_version: str,
        claim_id: str,
        confirm_code_hash: str,
    ) -> Dict[str, Any]:
        """Atomically grant one caller the right to send a request.

        The durable CLAIMED row is an outbox claim.  A process crash or an
        uncertain network outcome intentionally leaves it in CLAIMED so a
        retry cannot silently deliver a second request with a different code.
        A definite pre-delivery failure is moved to FAILED by
        :meth:`fail_request` and may then be claimed for another attempt.
        """
        if not task_id or not proposal_version or not claim_id or not confirm_code_hash:
            return {
                "ok": False,
                "acquired": False,
                "error": "task_id, proposal_version, claim_id and"
                         " confirm_code_hash are required",
            }
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                record = self._row(task_id)
                if record is None:
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "acquired": False,
                        "reason": "UNKNOWN_TASK",
                        "error": f"unknown task {task_id!r}",
                    }
                if (
                    record["status"] != PRODUCT_DRAFT
                    or record["proposal_version"] != proposal_version
                ):
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "acquired": False,
                        "reason": "NOT_CURRENT_DRAFT",
                        "status": record["status"],
                        "current_version": record["proposal_version"],
                        "error": "confirmation request requires the current"
                                 " PRODUCT_DRAFT version",
                    }

                request = self._conn.execute(
                    "SELECT * FROM confirmation_requests WHERE task_id = ?"
                    " AND proposal_version = ?",
                    (task_id, proposal_version),
                ).fetchone()
                if request is None:
                    self._conn.execute(
                        "INSERT INTO confirmation_requests (task_id,"
                        " proposal_version, state, claim_id, confirm_code_hash,"
                        " attempt_count, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (task_id, proposal_version, REQUEST_CLAIMED, claim_id,
                         confirm_code_hash, now, now),
                    )
                    attempt_count = 1
                elif request["state"] == REQUEST_FAILED:
                    cur = self._conn.execute(
                        "UPDATE confirmation_requests SET state = ?, claim_id = ?,"
                        " confirm_code_hash = ?, delivery_ref = NULL,"
                        " last_error = NULL, attempt_count = attempt_count + 1,"
                        " updated_at = ? WHERE task_id = ? AND proposal_version = ?"
                        " AND state = ?",
                        (REQUEST_CLAIMED, claim_id, confirm_code_hash, now,
                         task_id, proposal_version, REQUEST_FAILED),
                    )
                    if cur.rowcount != 1:  # pragma: no cover - transaction owns DB lock
                        self._conn.execute("ROLLBACK")
                        return {
                            "ok": False, "acquired": False,
                            "reason": "REQUEST_CONFLICT",
                            "error": "confirmation request claim changed concurrently",
                        }
                    attempt_count = int(request["attempt_count"]) + 1
                else:
                    state = request["state"]
                    self._conn.execute("COMMIT")
                    return {
                        "ok": state == REQUEST_DELIVERED,
                        "acquired": False,
                        "reason": (
                            "REQUEST_ALREADY_DELIVERED"
                            if state == REQUEST_DELIVERED
                            else "REQUEST_IN_PROGRESS"
                        ),
                        "request_state": state,
                        "status": record["status"],
                        "attempt_count": request["attempt_count"],
                        "error": (
                            "the confirmation request was already delivered"
                            if state == REQUEST_DELIVERED
                            else "another caller already owns this request send;"
                                 " no second message was sent"
                        ),
                    }
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "ok": True,
            "acquired": True,
            "task_id": task_id,
            "proposal_version": proposal_version,
            "request_state": REQUEST_CLAIMED,
            "attempt_count": attempt_count,
        }

    def complete_request_delivery(
        self,
        task_id: str,
        proposal_version: str,
        claim_id: str,
        delivery_ref: str = "",
    ) -> Dict[str, Any]:
        """Commit DELIVERED and PRODUCT_DRAFT -> WAITING atomically."""
        now = _now()
        evidence = f"dingtalk_delivery_ref={delivery_ref}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                request = self._conn.execute(
                    "SELECT * FROM confirmation_requests WHERE task_id = ?"
                    " AND proposal_version = ?",
                    (task_id, proposal_version),
                ).fetchone()
                if request is None:
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "reason": "REQUEST_NOT_CLAIMED",
                        "error": "no persistent request claim exists",
                    }
                if request["state"] == REQUEST_DELIVERED:
                    self._conn.execute("COMMIT")
                    return {
                        "ok": request["claim_id"] == claim_id,
                        "idempotent": request["claim_id"] == claim_id,
                        "reason": "REQUEST_ALREADY_DELIVERED",
                        "request_state": REQUEST_DELIVERED,
                        "status": WAITING_PRODUCT_CONFIRMATION,
                    }
                if (
                    request["state"] != REQUEST_CLAIMED
                    or request["claim_id"] != claim_id
                ):
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "reason": "REQUEST_CLAIM_MISMATCH",
                        "request_state": request["state"],
                        "error": "request finalization does not own the active claim",
                    }

                cur = self._conn.execute(
                    "UPDATE confirmations SET status = ?, requested_at = ?,"
                    " request_evidence = ?, confirm_code_hash = ?, updated_at = ?"
                    " WHERE task_id = ? AND proposal_version = ? AND status = ?",
                    (WAITING_PRODUCT_CONFIRMATION, now, evidence,
                     request["confirm_code_hash"], now, task_id,
                     proposal_version, PRODUCT_DRAFT),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "reason": "FINALIZE_CONFLICT",
                        "request_state": REQUEST_CLAIMED,
                        "error": "message was delivered but the workflow state"
                                 " could not be finalized; claim retained to"
                                 " prevent an automatic duplicate send",
                    }
                outbox = self._conn.execute(
                    "UPDATE confirmation_requests SET state = ?, delivery_ref = ?,"
                    " last_error = NULL, updated_at = ? WHERE task_id = ?"
                    " AND proposal_version = ? AND state = ? AND claim_id = ?",
                    (REQUEST_DELIVERED, delivery_ref or None, now, task_id,
                     proposal_version, REQUEST_CLAIMED, claim_id),
                )
                if outbox.rowcount != 1:  # pragma: no cover - same transaction
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "reason": "FINALIZE_CONFLICT",
                        "request_state": REQUEST_CLAIMED,
                        "error": "request outbox changed during finalization",
                    }
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "ok": True,
            "task_id": task_id,
            "proposal_version": proposal_version,
            "status": WAITING_PRODUCT_CONFIRMATION,
            "request_state": REQUEST_DELIVERED,
            "requested_at": now,
        }

    def fail_request(
        self,
        task_id: str,
        proposal_version: str,
        claim_id: str,
        error: str,
    ) -> Dict[str, Any]:
        """Release a claim only after a definite *not delivered* result."""
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE confirmation_requests SET state = ?, last_error = ?,"
                    " updated_at = ? WHERE task_id = ? AND proposal_version = ?"
                    " AND state = ? AND claim_id = ?",
                    (REQUEST_FAILED, str(error or "delivery failed")[:500], now,
                     task_id, proposal_version, REQUEST_CLAIMED, claim_id),
                )
                if cur.rowcount != 1:
                    request = self._conn.execute(
                        "SELECT state FROM confirmation_requests WHERE task_id = ?"
                        " AND proposal_version = ?",
                        (task_id, proposal_version),
                    ).fetchone()
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False, "retryable": False,
                        "reason": "REQUEST_CLAIM_MISMATCH",
                        "request_state": request["state"] if request else None,
                        "error": "failed delivery could not release its claim",
                    }
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "ok": True, "retryable": True,
            "reason": "DELIVERY_NOT_SENT",
            "request_state": REQUEST_FAILED,
            "status": PRODUCT_DRAFT,
        }

    def mark_waiting(
        self, task_id: str, proposal_version: str, request_evidence: str = "",
        confirm_code_hash: str = "",
    ) -> Dict[str, Any]:
        """PRODUCT_DRAFT -> WAITING_PRODUCT_CONFIRMATION (guarded).

        ``confirm_code_hash`` is the hash of the one-time confirmation code
        embedded in the outbound confirmation message. When set, a decision
        is only accepted if it carries the matching code — proof the decider
        actually replied to *this* confirmation request rather than the model
        reinterpreting an unrelated remark.
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE confirmations SET status = ?, requested_at = ?,"
                " request_evidence = ?, confirm_code_hash = ?, updated_at = ?"
                " WHERE task_id = ?"
                " AND status = ? AND proposal_version = ?",
                (WAITING_PRODUCT_CONFIRMATION, now, request_evidence,
                 confirm_code_hash or None, now,
                 task_id, PRODUCT_DRAFT, proposal_version),
            )
            if cur.rowcount == 0:
                row = self._row(task_id)
                if row is None:
                    return {"ok": False, "error": f"unknown task {task_id!r}"}
                return {
                    "ok": False,
                    "error": "confirmation request requires PRODUCT_DRAFT with the"
                             f" current version; task is in {row['status']}"
                             f" at version {row['proposal_version']!r}",
                    "status": row["status"],
                }
        return {"ok": True, "task_id": task_id,
                "status": WAITING_PRODUCT_CONFIRMATION, "requested_at": now}

    def apply_decision(
        self,
        task_id: str,
        proposal_version: str,
        decision: str,
        actor_id: str,
        owner_id: str,
        evidence: str = "",
        confirmation_code: str = "",
    ) -> Dict[str, Any]:
        """Apply the product owner's decision to a waiting confirmation.

        Rejections never change state and are always logged:
        * ``NOT_OWNER``      — actor is not the configured owner identity
        * ``STALE_VERSION``  — decision names a superseded proposal version
        * ``BAD_CODE``       — record carries a confirmation-code hash and the
                               supplied code is missing or does not match
        * ``NOT_WAITING``    — no confirmation request is pending
        * ``CONFLICT``       — terminal state disagrees with this decision
        * ``DUPLICATE``      — idempotent repeat; reported as ok, no change
        """
        canonical = normalize_decision(decision)
        if canonical is None:
            return {"ok": False, "applied": False,
                    "error": f"invalid decision {decision!r}; expected APPROVED"
                             " or NEEDS_REVISION"}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(task_id)
                if row is None:
                    self._log_decision(task_id, proposal_version, actor_id,
                                       canonical, False, "UNKNOWN_TASK")
                    self._conn.execute("COMMIT")
                    return {"ok": False, "applied": False, "reason": "UNKNOWN_TASK",
                            "error": f"unknown task {task_id!r}"}

                # Identity gate: fail closed on missing ids as well as mismatch.
                if not actor_id or not owner_id or actor_id != owner_id:
                    self._log_decision(task_id, proposal_version, actor_id,
                                       canonical, False, "NOT_OWNER")
                    self._conn.execute("COMMIT")
                    return {
                        "ok": False, "applied": False, "reason": "NOT_OWNER",
                        "status": row["status"],
                        "error": "decision rejected: replier is not the configured"
                                 " product owner (or owner identity is not"
                                 " configured); state unchanged",
                    }

                if proposal_version != row["proposal_version"]:
                    self._log_decision(task_id, proposal_version, actor_id,
                                       canonical, False, "STALE_VERSION")
                    self._conn.execute("COMMIT")
                    return {
                        "ok": False, "applied": False, "reason": "STALE_VERSION",
                        "status": row["status"],
                        "current_version": row["proposal_version"],
                        "error": f"decision names version {proposal_version!r} but"
                                 f" the current version is"
                                 f" {row['proposal_version']!r}; state unchanged",
                    }

                status = row["status"]

                # Confirmation-code gate: when the outbound request embedded a
                # one-time code, the decision must quote it back. The code is
                # only visible in the delivered chat message (never in tool
                # results or the DB), so a matching code proves the owner
                # replied to this specific request. Checked after the
                # NOT_WAITING short-circuit for drafts (no code exists yet).
                stored_code_hash = row["confirm_code_hash"] or ""
                if status != PRODUCT_DRAFT and stored_code_hash:
                    # Codes are lowercase hex; normalize so a hand-typed
                    # uppercase code from the owner is not falsely rejected.
                    supplied = str(confirmation_code or "").strip().lower()
                    if not supplied or code_hash(supplied) != stored_code_hash:
                        self._log_decision(task_id, proposal_version, actor_id,
                                           canonical, False, "BAD_CODE")
                        self._conn.execute("COMMIT")
                        return {
                            "ok": False, "applied": False, "reason": "BAD_CODE",
                            "status": status,
                            "error": "decision rejected: missing or wrong"
                                     " confirmation code; the code is included"
                                     " in the confirmation message the product"
                                     " owner received — state unchanged",
                        }

                if status == WAITING_PRODUCT_CONFIRMATION:
                    now = _now()
                    cur = self._conn.execute(
                        "UPDATE confirmations SET status = ?, decision = ?,"
                        " confirmed_at = ?, decision_evidence = ?, updated_at = ?"
                        " WHERE task_id = ? AND status = ? AND proposal_version = ?",
                        (_DECISION_TO_STATE[canonical], canonical, now, evidence,
                         now, task_id, WAITING_PRODUCT_CONFIRMATION,
                         proposal_version),
                    )
                    if cur.rowcount == 0:  # pragma: no cover - row re-read above
                        self._conn.execute("ROLLBACK")
                        return {"ok": False, "applied": False, "reason": "CONFLICT",
                                "error": "concurrent transition detected"}
                    self._log_decision(task_id, proposal_version, actor_id,
                                       canonical, True, "APPLIED")
                    self._conn.execute("COMMIT")
                    return {"ok": True, "applied": True, "decision": canonical,
                            "status": _DECISION_TO_STATE[canonical],
                            "confirmed_at": now}

                # Terminal / post-decision states: idempotent on identical
                # decision, conflict otherwise.
                effective = row["decision"]
                if status in (
                    TECH_DESIGN,
                    WAITING_TECH_DESIGN_CONFIRMATION,
                    TECH_DESIGN_NEEDS_REVISION,
                    TECH_DESIGN_APPROVED,
                ):
                    effective = effective or DECISION_APPROVED
                if effective == canonical:
                    self._log_decision(task_id, proposal_version, actor_id,
                                       canonical, True, "DUPLICATE")
                    self._conn.execute("COMMIT")
                    return {"ok": True, "applied": False, "idempotent": True,
                            "decision": effective, "status": status,
                            "confirmed_at": row["confirmed_at"]}
                if status == PRODUCT_DRAFT:
                    self._log_decision(task_id, proposal_version, actor_id,
                                       canonical, False, "NOT_WAITING")
                    self._conn.execute("COMMIT")
                    return {"ok": False, "applied": False, "reason": "NOT_WAITING",
                            "status": status,
                            "error": "no confirmation request is pending for this"
                                     " task; state unchanged"}
                self._log_decision(task_id, proposal_version, actor_id,
                                   canonical, False, "CONFLICT")
                self._conn.execute("COMMIT")
                return {"ok": False, "applied": False, "reason": "CONFLICT",
                        "status": status, "decision": row["decision"],
                        "error": f"task already resolved as {row['decision']} in"
                                 f" {status}; conflicting decision rejected"}
            except Exception:
                self._rollback_quietly()
                raise

    def log_rejection(
        self, task_id: str, proposal_version: str, actor_id: str,
        decision: str, reason: str,
    ) -> None:
        """Audit a tool-layer rejection (e.g. WRONG_CONVERSATION).

        Keeps the decision log complete for attempts the store itself never
        sees because an outer gate rejected them first.
        """
        canonical = normalize_decision(decision) or str(decision or "")[:32]
        with self._lock:
            self._log_decision(task_id, proposal_version, actor_id,
                               canonical, False, reason)

    def enter_tech_design(self, task_id: str) -> Dict[str, Any]:
        """PRODUCT_APPROVED -> TECH_DESIGN (guarded).

        This is the only path into TECH_DESIGN, which structurally forbids
        skipping product confirmation. TECH_DESIGN itself is non-coding and
        must pass the separate technical-design confirmation gate.
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE confirmations SET status = ?, updated_at = ?"
                " WHERE task_id = ? AND status = ?",
                (TECH_DESIGN, now, task_id, PRODUCT_APPROVED),
            )
            if cur.rowcount == 0:
                row = self._row(task_id)
                if row is None:
                    return {"ok": False, "error": f"unknown task {task_id!r}"}
                if row["status"] == TECH_DESIGN:
                    return {"ok": True, "idempotent": True, "task_id": task_id,
                            "status": TECH_DESIGN}
                return {
                    "ok": False, "status": row["status"],
                    "error": f"tech design requires PRODUCT_APPROVED; task is in"
                             f" {row['status']}",
                }
        return {"ok": True, "task_id": task_id, "status": TECH_DESIGN}

    def create_tech_design_draft(
        self,
        task_id: str,
        tech_design_version: str,
        tech_design_digest: str,
        tech_design_gate_json: str,
    ) -> Dict[str, Any]:
        """Register one immutable technical-design version.

        This transition deliberately stops at ``TECH_DESIGN``.  It records a
        reviewable design artifact and its structured, non-coding gate; a
        separate delivered request and owner decision are still required.
        """
        if not task_id or not tech_design_version or not tech_design_digest:
            return {
                "ok": False,
                "error": "task_id, tech_design_version and tech_design_digest"
                         " are required",
            }
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(task_id)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "UNKNOWN_TASK",
                            "error": f"unknown task {task_id!r}"}
                if row["status"] not in (TECH_DESIGN, TECH_DESIGN_NEEDS_REVISION):
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "status": row["status"],
                        "error": "a technical-design draft requires TECH_DESIGN"
                                 " or TECH_DESIGN_NEEDS_REVISION",
                    }
                duplicate = self._conn.execute(
                    "SELECT 1 FROM tech_design_versions WHERE task_id = ?"
                    " AND tech_design_version = ?",
                    (task_id, tech_design_version),
                ).fetchone()
                if duplicate:
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "reason": "IMMUTABLE_VERSION",
                        "error": f"technical-design version"
                                 f" {tech_design_version!r} is already registered;"
                                 " bump the version instead",
                    }
                if row["status"] == TECH_DESIGN and row["tech_design_version"]:
                    in_flight = self._conn.execute(
                        "SELECT 1 FROM confirmation_requests WHERE task_id = ?"
                        " AND proposal_version = ? AND state = ?",
                        (
                            task_id,
                            self._tech_request_version(
                                row["tech_design_version"]
                            ),
                            REQUEST_CLAIMED,
                        ),
                    ).fetchone()
                    if in_flight:
                        self._conn.execute("ROLLBACK")
                        return {
                            "ok": False,
                            "reason": "REQUEST_IN_PROGRESS",
                            "status": TECH_DESIGN,
                            "error": "a technical-design confirmation send is"
                                     " already in progress",
                        }
                cur = self._conn.execute(
                    "UPDATE confirmations SET status = ?,"
                    " tech_design_version = ?, tech_design_digest = ?,"
                    " tech_design_gate_json = ?, tech_requested_at = NULL,"
                    " tech_request_evidence = NULL,"
                    " tech_confirm_code_hash = NULL,"
                    " tech_confirmed_at = NULL, tech_decision = NULL,"
                    " tech_decision_evidence = NULL, updated_at = ?"
                    " WHERE task_id = ? AND status IN (?, ?)",
                    (
                        TECH_DESIGN,
                        tech_design_version,
                        tech_design_digest,
                        tech_design_gate_json,
                        now,
                        task_id,
                        TECH_DESIGN,
                        TECH_DESIGN_NEEDS_REVISION,
                    ),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "CONFLICT",
                            "error": "technical-design state changed concurrently"}
                self._conn.execute(
                    "INSERT INTO tech_design_versions (task_id,"
                    " tech_design_version, tech_design_digest,"
                    " tech_design_gate_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        task_id,
                        tech_design_version,
                        tech_design_digest,
                        tech_design_gate_json,
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "ok": True,
            "task_id": task_id,
            "status": TECH_DESIGN,
            "tech_design_version": tech_design_version,
            "coding_allowed": False,
            "worker_allowed": False,
        }

    def claim_tech_design_request(
        self,
        task_id: str,
        tech_design_version: str,
        claim_id: str,
        confirm_code_hash: str,
    ) -> Dict[str, Any]:
        """Durably claim one technical-design confirmation delivery."""
        request_version = self._tech_request_version(tech_design_version)
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(task_id)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return {"ok": False, "acquired": False,
                            "reason": "UNKNOWN_TASK",
                            "error": f"unknown task {task_id!r}"}
                if (
                    row["status"] != TECH_DESIGN
                    or row["tech_design_version"] != tech_design_version
                ):
                    self._conn.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "acquired": False,
                        "reason": "NOT_CURRENT_TECH_DESIGN",
                        "status": row["status"],
                        "current_version": row["tech_design_version"],
                        "error": "confirmation requires the current"
                                 " TECH_DESIGN version",
                    }
                request = self._conn.execute(
                    "SELECT * FROM confirmation_requests WHERE task_id = ?"
                    " AND proposal_version = ?",
                    (task_id, request_version),
                ).fetchone()
                if request is None:
                    self._conn.execute(
                        "INSERT INTO confirmation_requests (task_id,"
                        " proposal_version, state, claim_id, confirm_code_hash,"
                        " attempt_count, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (
                            task_id,
                            request_version,
                            REQUEST_CLAIMED,
                            claim_id,
                            confirm_code_hash,
                            now,
                            now,
                        ),
                    )
                    attempt_count = 1
                elif request["state"] == REQUEST_FAILED:
                    self._conn.execute(
                        "UPDATE confirmation_requests SET state = ?,"
                        " claim_id = ?, confirm_code_hash = ?,"
                        " delivery_ref = NULL, last_error = NULL,"
                        " attempt_count = attempt_count + 1, updated_at = ?"
                        " WHERE task_id = ? AND proposal_version = ?"
                        " AND state = ?",
                        (
                            REQUEST_CLAIMED,
                            claim_id,
                            confirm_code_hash,
                            now,
                            task_id,
                            request_version,
                            REQUEST_FAILED,
                        ),
                    )
                    attempt_count = int(request["attempt_count"]) + 1
                else:
                    self._conn.execute("COMMIT")
                    state = request["state"]
                    return {
                        "ok": state == REQUEST_DELIVERED,
                        "acquired": False,
                        "reason": (
                            "REQUEST_ALREADY_DELIVERED"
                            if state == REQUEST_DELIVERED
                            else "REQUEST_IN_PROGRESS"
                        ),
                        "request_state": state,
                        "status": row["status"],
                        "attempt_count": request["attempt_count"],
                    }
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "ok": True,
            "acquired": True,
            "task_id": task_id,
            "tech_design_version": tech_design_version,
            "request_state": REQUEST_CLAIMED,
            "attempt_count": attempt_count,
        }

    def complete_tech_design_request_delivery(
        self,
        task_id: str,
        tech_design_version: str,
        claim_id: str,
        delivery_ref: str = "",
    ) -> Dict[str, Any]:
        """Commit a delivered second-gate request and enter the waiting state."""
        request_version = self._tech_request_version(tech_design_version)
        now = _now()
        evidence = f"dingtalk_delivery_ref={delivery_ref}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                request = self._conn.execute(
                    "SELECT * FROM confirmation_requests WHERE task_id = ?"
                    " AND proposal_version = ?",
                    (task_id, request_version),
                ).fetchone()
                if request is None:
                    self._conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "REQUEST_NOT_CLAIMED",
                            "error": "no persistent request claim exists"}
                if request["state"] == REQUEST_DELIVERED:
                    same_claim = request["claim_id"] == claim_id
                    self._conn.execute("COMMIT")
                    return {
                        "ok": same_claim,
                        "idempotent": same_claim,
                        "reason": "REQUEST_ALREADY_DELIVERED",
                        "request_state": REQUEST_DELIVERED,
                        "status": WAITING_TECH_DESIGN_CONFIRMATION,
                    }
                if (
                    request["state"] != REQUEST_CLAIMED
                    or request["claim_id"] != claim_id
                ):
                    self._conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "REQUEST_CLAIM_MISMATCH",
                            "error": "request finalization does not own the claim"}
                cur = self._conn.execute(
                    "UPDATE confirmations SET status = ?,"
                    " tech_requested_at = ?, tech_request_evidence = ?,"
                    " tech_confirm_code_hash = ?, updated_at = ?"
                    " WHERE task_id = ? AND tech_design_version = ?"
                    " AND status = ?",
                    (
                        WAITING_TECH_DESIGN_CONFIRMATION,
                        now,
                        evidence,
                        request["confirm_code_hash"],
                        now,
                        task_id,
                        tech_design_version,
                        TECH_DESIGN,
                    ),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "FINALIZE_CONFLICT",
                            "error": "delivered request could not finalize state"}
                self._conn.execute(
                    "UPDATE confirmation_requests SET state = ?,"
                    " delivery_ref = ?, last_error = NULL, updated_at = ?"
                    " WHERE task_id = ? AND proposal_version = ?"
                    " AND state = ? AND claim_id = ?",
                    (
                        REQUEST_DELIVERED,
                        delivery_ref or None,
                        now,
                        task_id,
                        request_version,
                        REQUEST_CLAIMED,
                        claim_id,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "ok": True,
            "task_id": task_id,
            "tech_design_version": tech_design_version,
            "status": WAITING_TECH_DESIGN_CONFIRMATION,
            "request_state": REQUEST_DELIVERED,
            "requested_at": now,
        }

    def fail_tech_design_request(
        self,
        task_id: str,
        tech_design_version: str,
        claim_id: str,
        error: str,
    ) -> Dict[str, Any]:
        """Release a second-gate claim only after definite non-delivery."""
        result = self.fail_request(
            task_id,
            self._tech_request_version(tech_design_version),
            claim_id,
            error,
        )
        if result.get("ok"):
            result["status"] = TECH_DESIGN
        return result

    def apply_tech_design_decision(
        self,
        task_id: str,
        tech_design_version: str,
        decision: str,
        actor_id: str,
        owner_id: str,
        evidence: str = "",
        confirmation_code: str = "",
    ) -> Dict[str, Any]:
        """Apply the configured owner's decision at the technical-design gate."""
        canonical = normalize_decision(decision)
        if canonical is None:
            return {"ok": False, "applied": False,
                    "error": "invalid decision; expected APPROVED or"
                             " NEEDS_REVISION"}
        log_version = self._tech_request_version(tech_design_version)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(task_id)
                if row is None:
                    self._log_decision(task_id, log_version, actor_id,
                                       canonical, False, "UNKNOWN_TASK")
                    self._conn.execute("COMMIT")
                    return {"ok": False, "applied": False,
                            "reason": "UNKNOWN_TASK"}
                if not actor_id or not owner_id or actor_id != owner_id:
                    self._log_decision(task_id, log_version, actor_id,
                                       canonical, False, "NOT_OWNER")
                    self._conn.execute("COMMIT")
                    return {"ok": False, "applied": False,
                            "reason": "NOT_OWNER", "status": row["status"],
                            "error": "technical-design decision rejected:"
                                     " replier is not the configured owner"}
                if tech_design_version != row["tech_design_version"]:
                    self._log_decision(task_id, log_version, actor_id,
                                       canonical, False, "STALE_VERSION")
                    self._conn.execute("COMMIT")
                    return {
                        "ok": False,
                        "applied": False,
                        "reason": "STALE_VERSION",
                        "status": row["status"],
                        "current_version": row["tech_design_version"],
                    }
                status = row["status"]
                stored_code_hash = row["tech_confirm_code_hash"] or ""
                if status != TECH_DESIGN and stored_code_hash:
                    supplied = str(confirmation_code or "").strip().lower()
                    if not supplied or code_hash(supplied) != stored_code_hash:
                        self._log_decision(task_id, log_version, actor_id,
                                           canonical, False, "BAD_CODE")
                        self._conn.execute("COMMIT")
                        return {"ok": False, "applied": False,
                                "reason": "BAD_CODE", "status": status,
                                "error": "missing or wrong technical-design"
                                         " confirmation code"}
                if status == WAITING_TECH_DESIGN_CONFIRMATION:
                    target = (
                        TECH_DESIGN_APPROVED
                        if canonical == DECISION_APPROVED
                        else TECH_DESIGN_NEEDS_REVISION
                    )
                    now = _now()
                    cur = self._conn.execute(
                        "UPDATE confirmations SET status = ?,"
                        " tech_decision = ?, tech_confirmed_at = ?,"
                        " tech_decision_evidence = ?, updated_at = ?"
                        " WHERE task_id = ? AND status = ?"
                        " AND tech_design_version = ?",
                        (
                            target,
                            canonical,
                            now,
                            evidence,
                            now,
                            task_id,
                            WAITING_TECH_DESIGN_CONFIRMATION,
                            tech_design_version,
                        ),
                    )
                    if cur.rowcount != 1:
                        self._conn.execute("ROLLBACK")
                        return {"ok": False, "applied": False,
                                "reason": "CONFLICT"}
                    self._log_decision(task_id, log_version, actor_id,
                                       canonical, True, "APPLIED")
                    self._conn.execute("COMMIT")
                    return {"ok": True, "applied": True,
                            "decision": canonical, "status": target,
                            "confirmed_at": now,
                            "coding_allowed": False,
                            "worker_allowed": False}
                effective = row["tech_decision"]
                if effective == canonical and status in (
                    TECH_DESIGN_APPROVED,
                    TECH_DESIGN_NEEDS_REVISION,
                ):
                    self._log_decision(task_id, log_version, actor_id,
                                       canonical, True, "DUPLICATE")
                    self._conn.execute("COMMIT")
                    return {"ok": True, "applied": False, "idempotent": True,
                            "decision": effective, "status": status,
                            "coding_allowed": False, "worker_allowed": False}
                reason = "NOT_WAITING" if status == TECH_DESIGN else "CONFLICT"
                self._log_decision(task_id, log_version, actor_id,
                                   canonical, False, reason)
                self._conn.execute("COMMIT")
                return {"ok": False, "applied": False, "reason": reason,
                        "status": status,
                        "error": "no matching technical-design confirmation"
                                 " is waiting; state unchanged"}
            except Exception:
                self._rollback_quietly()
                raise

    # -- queries ---------------------------------------------------------------

    def resolve_task_key(
        self, logical_task_id: str, source_conversation: str,
    ) -> Optional[str]:
        """Resolve the internal key for a logical id in exactly one session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id FROM confirmations WHERE logical_task_id = ?"
                " AND source_conversation = ?",
                (logical_task_id, source_conversation),
            ).fetchone()
        return str(row["task_id"]) if row else None

    def logical_task_exists(self, logical_task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM confirmations WHERE logical_task_id = ? LIMIT 1",
                (logical_task_id,),
            ).fetchone()
        return row is not None

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._row(task_id)
        return self._to_dict(row) if row else None

    def list_pending(
        self, source_conversation: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Waiting product confirmations, optionally scoped to one session."""
        with self._lock:
            if source_conversation is None:
                cur = self._conn.execute(
                    "SELECT * FROM confirmations WHERE status = ?"
                    " ORDER BY requested_at",
                    (WAITING_PRODUCT_CONFIRMATION,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM confirmations WHERE status = ?"
                    " AND source_conversation = ? ORDER BY requested_at",
                    (WAITING_PRODUCT_CONFIRMATION, source_conversation),
                )
            return [self._to_dict(r) for r in cur.fetchall()]

    def list_pending_tech_design(
        self, source_conversation: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Waiting second-gate confirmations, optionally scoped to one session."""
        with self._lock:
            if source_conversation is None:
                cur = self._conn.execute(
                    "SELECT * FROM confirmations WHERE status = ?"
                    " ORDER BY tech_requested_at",
                    (WAITING_TECH_DESIGN_CONFIRMATION,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM confirmations WHERE status = ?"
                    " AND source_conversation = ? ORDER BY tech_requested_at",
                    (WAITING_TECH_DESIGN_CONFIRMATION, source_conversation),
                )
            return [self._to_dict(r) for r in cur.fetchall()]

    def list_versions(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM confirmation_versions WHERE task_id = ?"
                " ORDER BY created_at",
                (task_id,),
            )
            return [self._to_dict(r) for r in cur.fetchall()]

    def list_tech_design_versions(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tech_design_versions WHERE task_id = ?"
                " ORDER BY created_at",
                (task_id,),
            )
            return [self._to_dict(r) for r in cur.fetchall()]

    def get_request_status(
        self, task_id: str, proposal_version: str,
    ) -> Optional[Dict[str, Any]]:
        """Return explainable outbox state without claim ids or code hashes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, proposal_version, state, delivery_ref,"
                " attempt_count, last_error, created_at, updated_at"
                " FROM confirmation_requests WHERE task_id = ?"
                " AND proposal_version = ?",
                (task_id, proposal_version),
            ).fetchone()
        return self._to_dict(row) if row else None

    def get_tech_design_request_status(
        self, task_id: str, tech_design_version: str,
    ) -> Optional[Dict[str, Any]]:
        row = self.get_request_status(
            task_id, self._tech_request_version(tech_design_version)
        )
        if row:
            row["tech_design_version"] = tech_design_version
            row.pop("proposal_version", None)
        return row

    def list_unsettled_requests(
        self, source_conversation: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Unsettled outbox rows, optionally scoped to one origin session."""
        with self._lock:
            query = (
                "SELECT r.task_id, r.proposal_version, r.state,"
                " r.delivery_ref, r.attempt_count, r.last_error,"
                " r.created_at, r.updated_at"
                " FROM confirmation_requests AS r"
                " JOIN confirmations AS c ON c.task_id = r.task_id"
                " WHERE r.state IN (?, ?)"
            )
            params: tuple[Any, ...] = (REQUEST_CLAIMED, REQUEST_FAILED)
            if source_conversation is not None:
                query += " AND c.source_conversation = ?"
                params += (source_conversation,)
            query += " ORDER BY r.updated_at, r.task_id"
            cur = self._conn.execute(query, params)
            return [self._to_dict(r) for r in cur.fetchall()]

    def decision_history(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM decision_log WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
            return [self._to_dict(r) for r in cur.fetchall()]


def summarize(record: Dict[str, Any]) -> str:
    """One-line low-sensitivity summary used in tool results and reports."""
    return json.dumps(
        {k: record.get(k) for k in (
            "task_id", "proposal_version", "status", "decision",
            "requested_at", "confirmed_at",
        )},
        ensure_ascii=False,
    )
