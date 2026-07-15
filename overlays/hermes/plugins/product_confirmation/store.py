"""Durable product-confirmation state machine backed by SQLite.

Implements the contract in
``docs/hermes1-agong-dev-loop_20260707/profile-evaluation/02_product_confirmation_contract.md``:

    PRODUCT_DRAFT
      -> WAITING_PRODUCT_CONFIRMATION
          -> PRODUCT_NEEDS_REVISION -> PRODUCT_DRAFT
          -> PRODUCT_APPROVED -> TECH_DESIGN

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
        if "confirm_code_hash" not in cols:
            self._conn.execute(
                "ALTER TABLE confirmations ADD COLUMN confirm_code_hash TEXT"
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
    ) -> Dict[str, Any]:
        """Create a new task in PRODUCT_DRAFT, or re-draft after NEEDS_REVISION.

        Each (task_id, proposal_version) pair is registered exactly once so a
        version uniquely locates one proposal. Re-drafting is only legal from
        PRODUCT_DRAFT / PRODUCT_NEEDS_REVISION — an in-flight confirmation
        (WAITING) or an approved task cannot be silently replaced.
        """
        if not task_id or not proposal_version:
            return {"ok": False, "error": "task_id and proposal_version are required"}
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
                        "INSERT INTO confirmations (task_id, source_conversation,"
                        " product_owner, proposal_version, proposal_digest, status,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (task_id, source_conversation, product_owner,
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
                if status == TECH_DESIGN:
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
        entering tech design (and thus coding) from PRODUCT_DRAFT or
        WAITING_PRODUCT_CONFIRMATION.
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

    # -- queries ---------------------------------------------------------------

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._row(task_id)
        return self._to_dict(row) if row else None

    def list_pending(self) -> List[Dict[str, Any]]:
        """All confirmations still waiting on the product owner (restart-safe)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM confirmations WHERE status = ? ORDER BY requested_at",
                (WAITING_PRODUCT_CONFIRMATION,),
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

    def list_unsettled_requests(self) -> List[Dict[str, Any]]:
        """Claims needing reconciliation and failed sends eligible for retry."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT task_id, proposal_version, state, delivery_ref,"
                " attempt_count, last_error, created_at, updated_at"
                " FROM confirmation_requests WHERE state IN (?, ?)"
                " ORDER BY updated_at, task_id",
                (REQUEST_CLAIMED, REQUEST_FAILED),
            )
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
