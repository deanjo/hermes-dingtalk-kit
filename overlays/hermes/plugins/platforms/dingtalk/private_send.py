"""Admin-gated DingTalk 1:1 private-message tool for Hermes.

Exposes a single agent tool, ``dingtalk_send_private_message``, that lets the
agent deliver a 1:1 private DingTalk message **only** on behalf of a configured
administrator. The capability is deliberately generic and config-driven:

- The admin allowlist is read from the ``DINGTALK_PRIVATE_MESSAGE_ADMINS`` env
  var (comma-separated). Each entry may be a raw DingTalk id (staff/sender id)
  or its lowercase SHA-256 hex hash. An empty allowlist denies everyone
  (fail-closed). No identity is hardcoded in source.
- The commanding user's identity is taken from the task-local session
  environment (``HERMES_SESSION_USER_ID`` / ``HERMES_SESSION_USER_ID_ALT``),
  which the gateway injects from the real DingTalk event. It is NOT a
  model-provided argument, so the model cannot forge "who is asking".
- Sending is a two-step ``prepare`` -> ``confirm`` flow. ``prepare`` returns a
  preview plus a short random confirm code; ``confirm`` must arrive in a LATER
  human turn (different ``HERMES_SESSION_MESSAGE_ID``) and carry that code.
  Same-turn self-confirmation is rejected.

The actual delivery uses the DingTalk enterprise robot 1:1 endpoint
``/v1.0/robot/oToMessages/batchSend`` with ``msgKey=sampleMarkdown``.

Recipient resolution accepts an explicit ``user_id`` (most robust) or, when the
``qyapi_addresslist_search`` scope is granted, a ``recipient_name`` resolved via
``/v1.0/contact/users/search`` (a unique match is required).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

try:  # optional dep; the platform plugin already requires it at runtime
    import httpx
except Exception:  # noqa: BLE001 - degrade gracefully when httpx is absent
    httpx = None  # type: ignore[assignment]

_DEFAULT_TIMEOUT_SECONDS = 15.0
_CONFIRM_TTL_SECONDS = 600  # 10 minutes
_CONFIRM_CODE_DIGITS = 4
_MAX_TEXT_CHARS = 4000
_ADMINS_ENV = "DINGTALK_PRIVATE_MESSAGE_ADMINS"
_STATE_DIR_ENV = "DINGTALK_KIT_STATE_DIR"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _hash_identifier(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_dir() -> str:
    base = os.getenv(_STATE_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".hermes-dingtalk-kit"
    )
    os.makedirs(base, exist_ok=True)
    return base


def _db_path() -> str:
    return os.path.join(_state_dir(), "dingtalk_private_send.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS preview_tokens (
            token TEXT PRIMARY KEY,
            created_epoch REAL,
            sender_hash TEXT,
            session_id TEXT,
            issued_message_id TEXT,
            recipient_user_id TEXT,
            recipient_hash TEXT,
            title TEXT,
            text TEXT,
            confirm_code TEXT,
            consumed INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS send_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_epoch REAL,
            action TEXT,
            decision TEXT,
            reason_code TEXT,
            sender_hash TEXT,
            recipient_hash TEXT,
            session_id TEXT
        )"""
    )
    return conn


def _audit(
    action: str,
    decision: str,
    reason_code: str,
    *,
    sender_hash: str = "",
    recipient_hash: str = "",
    session_id: str = "",
) -> None:
    try:
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT INTO send_audit (created_epoch, action, decision, reason_code,"
                " sender_hash, recipient_hash, session_id) VALUES (?,?,?,?,?,?,?)",
                (time.time(), action, decision, reason_code, sender_hash, recipient_hash, session_id),
            )
        conn.close()
    except Exception:  # noqa: BLE001 - audit must never break the tool
        pass


# --------------------------------------------------------------------------- #
# identity + admin gate (never trust model-provided identity)
# --------------------------------------------------------------------------- #
class _Identity:
    __slots__ = ("user_id", "user_id_alt", "user_name", "session_id", "message_id")

    def __init__(self) -> None:
        try:
            from gateway.session_context import get_session_env
        except Exception:  # noqa: BLE001 - outside gateway (e.g. unit tests)
            def get_session_env(_key: str, _default: str = "") -> str:  # type: ignore
                return os.getenv(_key, _default)
        self.user_id = get_session_env("HERMES_SESSION_USER_ID", "") or ""
        self.user_id_alt = get_session_env("HERMES_SESSION_USER_ID_ALT", "") or ""
        self.user_name = get_session_env("HERMES_SESSION_USER_NAME", "") or ""
        self.session_id = get_session_env("HERMES_SESSION_ID", "") or ""
        self.message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "") or ""

    def candidates(self) -> List[str]:
        vals: List[str] = []
        for raw in (self.user_id, self.user_id_alt):
            if raw:
                vals.append(raw)
                vals.append(_hash_identifier(raw))
        return vals

    def sender_hash(self) -> str:
        return _hash_identifier(self.user_id_alt or self.user_id)


def _admin_allowlist() -> set[str]:
    raw = os.getenv(_ADMINS_ENV, "") or ""
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def _is_admin(identity: _Identity) -> bool:
    allow = _admin_allowlist()
    if not allow:  # fail-closed: no configured admin => nobody is allowed
        return False
    return bool(set(identity.candidates()) & allow)


# --------------------------------------------------------------------------- #
# DingTalk API
# --------------------------------------------------------------------------- #
_TOKEN_CACHE: Dict[str, Any] = {"token": "", "expires": 0.0}


def _robot_code() -> str:
    return (os.getenv("DINGTALK_ROBOT_CODE") or os.getenv("DINGTALK_CLIENT_ID") or "").strip()


def _access_token() -> Tuple[Optional[str], str]:
    if httpx is None:
        return None, "httpx_unavailable"
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires"] - 60 > now:
        return _TOKEN_CACHE["token"], ""
    cid = os.getenv("DINGTALK_CLIENT_ID", "")
    csec = os.getenv("DINGTALK_CLIENT_SECRET", "")
    if not cid or not csec:
        return None, "missing_dingtalk_credentials"
    try:
        resp = httpx.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": cid, "appSecret": csec},
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"token_error:{type(exc).__name__}"
    if resp.status_code >= 400:
        return None, "token_rejected"
    data = resp.json() if resp.content else {}
    token = str(data.get("accessToken") or "")
    if not token:
        return None, "token_absent"
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires"] = now + float(data.get("expireIn") or 7200)
    return token, ""


def _search_user_by_name(name: str) -> Dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "error_code": "missing_recipient_name"}
    token, err = _access_token()
    if not token:
        return {"ok": False, "error_code": err}
    try:
        resp = httpx.post(  # type: ignore[union-attr]
            "https://api.dingtalk.com/v1.0/contact/users/search",
            headers={"x-acs-dingtalk-access-token": token},
            json={"queryWord": name, "offset": 0, "size": 20},
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_code": f"search_error:{type(exc).__name__}"}
    if resp.status_code >= 400:
        data = resp.json() if resp.content else {}
        return {"ok": False, "error_code": str(data.get("code") or resp.status_code)}
    data = resp.json() if resp.content else {}
    ids: List[str] = []
    for item in data.get("list") or []:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
        elif isinstance(item, dict):
            uid = item.get("userId") or item.get("userid") or item.get("user_id")
            if uid:
                ids.append(str(uid).strip())
    ids = list(dict.fromkeys([i for i in ids if i]))
    return {"ok": True, "user_ids": ids}


def _send_private_markdown(user_id: str, title: str, text: str) -> Dict[str, Any]:
    if httpx is None:
        return {"ok": False, "error_code": "httpx_unavailable"}
    token, err = _access_token()
    if not token:
        return {"ok": False, "error_code": err}
    robot_code = _robot_code()
    if not robot_code:
        return {"ok": False, "error_code": "missing_robot_code"}
    body = {
        "robotCode": robot_code,
        "userIds": [user_id],
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
    }
    try:
        resp = httpx.post(
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": token},
            json=body,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_code": f"send_error:{type(exc).__name__}"}
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        return {"ok": False, "error_code": str(data.get("code") or resp.status_code)}
    invalid = data.get("invalidStaffIdList") or []
    flow = data.get("flowControlledStaffIdList") or []
    if invalid:
        return {"ok": False, "error_code": "recipient_invalid", "detail": invalid}
    if flow:
        return {"ok": False, "error_code": "flow_controlled", "detail": flow}
    return {"ok": True, "process_query_key": data.get("processQueryKey", "")}


# --------------------------------------------------------------------------- #
# tool schema + handler
# --------------------------------------------------------------------------- #
DINGTALK_SEND_PRIVATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["prepare", "confirm", "cancel"],
            "description": "prepare: build a preview + confirm code; confirm: send after the "
            "admin replies the code in a new turn; cancel: drop a pending preview.",
        },
        "user_id": {
            "type": "string",
            "description": "Recipient DingTalk userId (org staff id). Most reliable; preferred.",
        },
        "recipient_name": {
            "type": "string",
            "description": "Recipient display name; resolved via contact search when user_id is "
            "absent. Requires the qyapi_addresslist_search scope and a unique match.",
        },
        "title": {"type": "string", "description": "Message title (DingTalk markdown card title)."},
        "text": {"type": "string", "description": "Message body in DingTalk markdown."},
        "token": {"type": "string", "description": "Preview token returned by a prior prepare call."},
        "confirm_code": {
            "type": "string",
            "description": "The confirm code the admin relayed back; required for confirm.",
        },
    },
    "required": ["action"],
}


def _result(payload: Dict[str, Any]) -> str:
    try:
        from tools.registry import tool_result
        return tool_result(payload)
    except Exception:  # noqa: BLE001 - outside gateway (unit tests)
        return json.dumps(payload, ensure_ascii=False)


def _error(message: str) -> str:
    try:
        from tools.registry import tool_error
        return tool_error(message)
    except Exception:  # noqa: BLE001
        return json.dumps({"error": message}, ensure_ascii=False)


def _handle_prepare(identity: _Identity, args: Dict[str, Any]) -> str:
    title = str(args.get("title") or "").strip()
    text = str(args.get("text") or "").strip()
    if not text:
        return _error("prepare requires a non-empty 'text'.")
    if len(text) > _MAX_TEXT_CHARS:
        return _error(f"'text' too long ({len(text)} > {_MAX_TEXT_CHARS}).")
    if not title:
        title = "消息"

    user_id = str(args.get("user_id") or "").strip()
    if not user_id:
        name = str(args.get("recipient_name") or "").strip()
        if not name:
            return _error("prepare requires 'user_id' or 'recipient_name'.")
        search = _search_user_by_name(name)
        if not search.get("ok"):
            return _error(f"recipient lookup failed: {search.get('error_code')}")
        matches = search.get("user_ids") or []
        if not matches:
            return _error(f"recipient_not_found: no DingTalk user matched '{name}'.")
        if len(matches) > 1:
            return _error(f"recipient_ambiguous: '{name}' matched {len(matches)} users; pass user_id.")
        user_id = matches[0]

    token = secrets.token_hex(8)
    code = "".join(secrets.choice("0123456789") for _ in range(_CONFIRM_CODE_DIGITS))
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO preview_tokens (token, created_epoch, sender_hash, session_id,"
            " issued_message_id, recipient_user_id, recipient_hash, title, text, confirm_code,"
            " consumed) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (
                token,
                time.time(),
                identity.sender_hash(),
                identity.session_id,
                identity.message_id,
                user_id,
                _hash_identifier(user_id),
                title,
                text,
                code,
            ),
        )
    conn.close()
    _audit("prepare", "issued", "ok", sender_hash=identity.sender_hash(),
           recipient_hash=_hash_identifier(user_id), session_id=identity.session_id)
    preview = text if len(text) <= 200 else text[:200] + "…"
    return _result({
        "status": "preview",
        "token": token,
        "confirm_code": code,
        "recipient_user_id": user_id,
        "title": title,
        "preview": preview,
        "next": "Show this preview and confirm code to the admin. To send, call confirm with this "
                "token AND the code AFTER the admin replies it in a new message.",
    })


def _handle_confirm(identity: _Identity, args: Dict[str, Any]) -> str:
    token = str(args.get("token") or "").strip()
    code = str(args.get("confirm_code") or "").strip()
    if not token or not code:
        return _error("confirm requires 'token' and 'confirm_code'.")
    conn = _connect()
    row = conn.execute(
        "SELECT created_epoch, sender_hash, session_id, issued_message_id, recipient_user_id,"
        " title, text, confirm_code, consumed FROM preview_tokens WHERE token=?",
        (token,),
    ).fetchone()
    if not row:
        conn.close()
        return _error("unknown or expired token; call prepare again.")
    (created, sender_hash, session_id, issued_msg_id, recipient_user_id,
     title, text, real_code, consumed) = row

    def _reject(reason: str, msg: str) -> str:
        _audit("confirm", "denied", reason, sender_hash=identity.sender_hash(),
               recipient_hash=_hash_identifier(recipient_user_id), session_id=identity.session_id)
        conn.close()
        return _error(msg)

    if consumed:
        return _reject("already_consumed", "this token was already used; call prepare again.")
    if time.time() - float(created) > _CONFIRM_TTL_SECONDS:
        return _reject("expired", "preview expired; call prepare again.")
    if sender_hash != identity.sender_hash():
        return _reject("sender_mismatch", "only the admin who prepared this send may confirm it.")
    if session_id and identity.session_id and session_id != identity.session_id:
        return _reject("session_mismatch", "confirm must happen in the same conversation.")
    # cross-turn gate: confirm must arrive in a LATER human message than prepare
    if issued_msg_id and identity.message_id and issued_msg_id == identity.message_id:
        return _reject("needs_human_confirm_turn",
                       "same-turn self-confirm is not allowed; wait for the admin to reply the code.")
    if code != real_code:
        return _reject("bad_confirm_code", "confirm code does not match.")

    result = _send_private_markdown(recipient_user_id, title, text)
    with conn:
        conn.execute("UPDATE preview_tokens SET consumed=1 WHERE token=?", (token,))
    conn.close()
    if not result.get("ok"):
        _audit("confirm", "send_failed", str(result.get("error_code")),
               sender_hash=identity.sender_hash(), recipient_hash=_hash_identifier(recipient_user_id),
               session_id=identity.session_id)
        return _error(f"send failed: {result.get('error_code')} {result.get('detail', '')}".strip())
    _audit("confirm", "sent", "ok", sender_hash=identity.sender_hash(),
           recipient_hash=_hash_identifier(recipient_user_id), session_id=identity.session_id)
    return _result({"status": "sent", "recipient_user_id": recipient_user_id,
                    "process_query_key": result.get("process_query_key", "")})


def _handle_cancel(identity: _Identity, args: Dict[str, Any]) -> str:
    token = str(args.get("token") or "").strip()
    if not token:
        return _error("cancel requires 'token'.")
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM preview_tokens WHERE token=? AND sender_hash=?",
                     (token, identity.sender_hash()))
    conn.close()
    return _result({"status": "cancelled", "token": token})


def handle_dingtalk_send_private(args: Dict[str, Any], **_kw: Any) -> str:
    """Tool handler. Admin-gated; never trusts model-provided identity."""
    args = args or {}
    identity = _Identity()
    if not _is_admin(identity):
        _audit("gate", "denied", "not_admin", sender_hash=identity.sender_hash(),
               session_id=identity.session_id)
        return _error(
            "Not authorized: only a configured administrator may send private DingTalk messages. "
            "The requesting user is not in DINGTALK_PRIVATE_MESSAGE_ADMINS."
        )
    action = str(args.get("action") or "").strip().lower()
    if action == "prepare":
        return _handle_prepare(identity, args)
    if action == "confirm":
        return _handle_confirm(identity, args)
    if action == "cancel":
        return _handle_cancel(identity, args)
    return _error("invalid 'action'; expected one of: prepare, confirm, cancel.")


def check_private_send_available() -> bool:
    """Tool stays registered even when unconfigured; runtime gate enforces admin."""
    return bool(os.getenv("DINGTALK_CLIENT_ID") and os.getenv("DINGTALK_CLIENT_SECRET"))


def register_private_send_tool(ctx) -> None:
    """Register the private-message tool on the shared plugin context."""
    ctx.register_tool(
        name="dingtalk_send_private_message",
        toolset="dingtalk",
        schema=DINGTALK_SEND_PRIVATE_SCHEMA,
        handler=handle_dingtalk_send_private,
        check_fn=check_private_send_available,
        emoji="✉️",
    )
