"""DingTalk inbound reply and forwarded-message helpers."""

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger(__name__)

_FORWARD_DIAG_MAX_ITEMS = 8
_DINGTALK_PLACEHOLDER_TEXTS = {"[图文消息]", "群聊的聊天记录"}

# Internal sentinel meaning "the user replied to an earlier message but DingTalk
# did not deliver the original text". The adapter consumes it before dispatch.
_REPLY_ORIGINAL_UNAVAILABLE = "\x00__HERMES_REPLY_ORIGINAL_UNAVAILABLE__\x00"
_REPLY_ORIGINAL_CLARIFICATION = (
    "我暂时拿不到你引用消息的原文。请把关键原文贴在消息里，或重新描述要我处理的内容。"
)
_CARD_REPLY_DB_NAME = "dingtalk_card_replies.db"
_CARD_REPLY_MAX_ROWS = 5000
_DINGTALK_STATE_DIR_ENV = "DINGTALK_KIT_STATE_DIR"
_WEBHOOK_REPLY_MATCH_SLOP_MS = 5000
_WEBHOOK_MESSAGE_ID_KEYS = (
    "msgId",
    "msg_id",
    "messageId",
    "message_id",
    "openMessageId",
    "open_message_id",
    "carrierId",
    "carrier_id",
)


def _safe_keys(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value.keys())


def _safe_list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def _raw_content(data: Any) -> Any:
    if not isinstance(data, dict):
        return None
    return _jsonish(data.get("content") or data.get("contentJson"))


def _is_placeholder_text(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped in _DINGTALK_PLACEHOLDER_TEXTS:
        return True
    return all(
        not line.strip() or line.strip().endswith("[图文消息]")
        for line in stripped.splitlines()
    )


def _walk_dicts(value: Any, *, depth: int = 0):
    if depth > 6:
        return
    value = _jsonish(value)
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:50]:
            yield from _walk_dicts(child, depth=depth + 1)


def _forward_diag_from_raw(data: Any) -> Dict[str, Any]:
    """Return a low-sensitive callback shape summary for forwarded content."""
    summary: Dict[str, Any] = {"data_type": type(data).__name__}
    if not isinstance(data, dict):
        return summary

    content = _raw_content(data)
    text = data.get("text")
    chat_record = content.get("chatRecord") if isinstance(content, dict) else None
    rich_text = content.get("richText") if isinstance(content, dict) else None
    summary.update(
        {
            "top_keys": _safe_keys(data),
            "msgtype": data.get("msgtype") or data.get("messageType"),
            "is_forward_msg": bool(data.get("isForwardMsg")),
            "content_type": type(content).__name__ if content is not None else None,
            "content_keys": _safe_keys(content),
            "text_keys": _safe_keys(text),
            "text_len": len(str(text.get("content") or "")) if isinstance(text, dict) else 0,
            "summary_len": len(str(content.get("summary") or "")) if isinstance(content, dict) else 0,
            "chat_record_type": type(chat_record).__name__ if chat_record is not None else None,
            "chat_record_count": _safe_list_len(chat_record),
            "rich_text_type": type(rich_text).__name__ if rich_text is not None else None,
            "rich_text_count": _safe_list_len(rich_text),
        }
    )
    if isinstance(chat_record, list) and chat_record:
        item_keys = []
        item_types = []
        for item in chat_record[:_FORWARD_DIAG_MAX_ITEMS]:
            if isinstance(item, dict):
                item_keys.append(_safe_keys(item))
                item_types.append(
                    item.get("msgtype")
                    or item.get("msgType")
                    or item.get("type")
                    or type(item).__name__
                )
            else:
                item_types.append(type(item).__name__)
        summary["chat_record_item_keys"] = item_keys
        summary["chat_record_item_types"] = item_types
    if isinstance(rich_text, list):
        summary["rich_text_item_types"] = [
            item.get("type") if isinstance(item, dict) else type(item).__name__
            for item in rich_text[:_FORWARD_DIAG_MAX_ITEMS]
        ]

    download_keys: Set[str] = set()
    download_ref_count = 0
    nested_msg_types: Set[str] = set()
    for obj in _walk_dicts(data):
        for key, value in obj.items():
            key_s = str(key)
            if key_s in {"downloadCode", "pictureDownloadCode", "download_code"} and value:
                download_keys.add(key_s)
                download_ref_count += 1
            if key_s in {"msgType", "msgtype"} and isinstance(value, str):
                nested_msg_types.add(value)
    summary["download_ref_count"] = download_ref_count
    summary["download_ref_keys"] = sorted(download_keys)
    summary["nested_msg_types"] = sorted(nested_msg_types)[:_FORWARD_DIAG_MAX_ITEMS]
    return summary


def _chat_record_items(value: Any, *, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 4:
        return []
    parsed = _jsonish(value)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in ("chatRecord", "chatRecords", "records", "items", "messages", "list"):
            nested = _chat_record_items(parsed.get(key), depth=depth + 1)
            if nested:
                return nested
        if any(key in parsed for key in ("content", "text", "plainText", "summary", "title", "msgType", "msgtype")):
            return [parsed]
    return []


def _non_placeholder_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or _is_placeholder_text(text):
        return ""
    return text


def _forwarded_chat_text_from_raw(data: Any, *, include_senders: bool = True) -> str:
    """Extract user-provided forwarded chat text from DingTalk chatRecord payloads."""
    content = _raw_content(data)
    if not isinstance(content, dict):
        return ""
    chat_record = content.get("chatRecord")
    records = _chat_record_items(chat_record)
    if not records:
        if isinstance(chat_record, str):
            fallback = _non_placeholder_text(chat_record)
            if fallback:
                return fallback
        return _non_placeholder_text(content.get("summary"))

    lines: List[str] = []
    for item in records:
        sender = (
            item.get("senderNick")
            or item.get("senderName")
            or item.get("fromNick")
            or item.get("name")
            or ""
        )
        msg_type = str(item.get("msgtype") or item.get("msgType") or item.get("type") or "").strip()
        text = _text_from_forward_record(item)
        if not text and msg_type:
            text = f"[{msg_type}]"
        if not text:
            continue
        prefix = f"{sender}: " if include_senders and sender else ""
        lines.append(f"{prefix}{text}")
    return "\n".join(lines).strip()


def _text_from_forward_record(item: Dict[str, Any]) -> str:
    for key in ("text", "plainText", "summary", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    content = _jsonish(item.get("content"))
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, dict):
        return ""

    for key in ("content", "text", "plainText", "summary", "title"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("content") or value.get("text")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()

    rich_text = content.get("richText") or content.get("rich_text")
    if isinstance(rich_text, list):
        parts = []
        for part in rich_text:
            if isinstance(part, dict):
                part_text = part.get("text") or part.get("content")
                if isinstance(part_text, str) and part_text.strip():
                    parts.append(part_text.strip())
        if parts:
            return " ".join(parts)
    return ""


def _log_forward_diag(stage: str, data: Any) -> None:
    summary = _forward_diag_from_raw(data)
    if not (
        summary.get("msgtype") in {"chatRecord", "richText"}
        or summary.get("is_forward_msg")
        or summary.get("chat_record_count")
        or summary.get("download_ref_count")
    ):
        return
    logger.warning(
        "DINGTALK_FORWARD_DIAG stage=%s summary=%s",
        stage,
        json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
    )


def _get_text_extensions(message: "ChatbotMessage") -> Dict[str, Any]:
    text = getattr(message, "text", None)
    extensions = getattr(text, "extensions", None)
    return extensions if isinstance(extensions, dict) else {}


def _field(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _delivery_carrier_ids(response: Any) -> List[str]:
    """Extract successful carrier ids from DingTalk's delivery response."""
    body = _field(response, "body") or response
    result = _field(body, "result")
    items = result if isinstance(result, (list, tuple)) else [result or body]
    carrier_ids: List[str] = []
    for item in items[:32]:
        success = _field(item, "success")
        if success is False or str(success).lower() in {"false", "0"}:
            continue
        raw_ids = _field(item, "carrier_id", "carrierId")
        values = raw_ids if isinstance(raw_ids, (list, tuple)) else [raw_ids]
        for value in values:
            carrier_id = str(value or "").strip()
            if carrier_id and carrier_id not in carrier_ids:
                carrier_ids.append(carrier_id)
    return carrier_ids


def _webhook_response_message_ids(response_body: Any) -> List[str]:
    """Extract exact message ids when a webhook implementation returns them."""
    message_ids: List[str] = []
    for item in _walk_dicts(response_body):
        for key in _WEBHOOK_MESSAGE_ID_KEYS:
            value = item.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            for candidate in values:
                message_id = str(candidate or "").strip()
                if message_id and message_id not in message_ids:
                    message_ids.append(message_id)
    return message_ids[:32]


def _coerce_epoch_ms(value: Any) -> Optional[int]:
    """Normalize DingTalk's repliedMsg.createdAt to epoch milliseconds."""
    if isinstance(value, bool):
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    if epoch < 100_000_000_000:  # seconds
        epoch *= 1000
    elif epoch > 10_000_000_000_000:  # microseconds or finer
        epoch /= 1000
    return int(epoch)


class CardReplyStore:
    """Restart-safe, chat-scoped lookup from card carrier id to latest text."""

    def __init__(self, state_dir: Optional[str] = None, *, max_rows: int = _CARD_REPLY_MAX_ROWS):
        self._state_dir = os.fspath(state_dir) if state_dir else None
        self._max_rows = max(1, int(max_rows))

    def _db_path(self) -> str:
        base = self._state_dir or os.getenv(_DINGTALK_STATE_DIR_ENV)
        if not base:
            hermes_home = (os.getenv("HERMES_HOME") or "").strip()
            base = (
                os.path.join(hermes_home, "dingtalk-kit")
                if hermes_home
                else os.path.join(os.path.expanduser("~"), ".hermes-dingtalk-kit")
            )
        os.makedirs(base, mode=0o700, exist_ok=True)
        return os.path.join(base, _CARD_REPLY_DB_NAME)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), timeout=5)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS card_replies (
                chat_id TEXT NOT NULL,
                carrier_id TEXT NOT NULL,
                out_track_id TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_epoch REAL NOT NULL,
                PRIMARY KEY (chat_id, carrier_id)
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS card_replies_track "
            "ON card_replies (chat_id, out_track_id)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS webhook_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                request_started_ms INTEGER NOT NULL,
                request_finished_ms INTEGER NOT NULL,
                content TEXT NOT NULL,
                updated_epoch REAL NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS webhook_replies_message "
            "ON webhook_replies (chat_id, message_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS webhook_replies_time "
            "ON webhook_replies (chat_id, request_started_ms, request_finished_ms)"
        )
        return conn

    def remember_delivery(
        self,
        chat_id: str,
        out_track_id: str,
        content: str,
        delivery_response: Any,
    ) -> bool:
        carrier_ids = _delivery_carrier_ids(delivery_response)
        if not chat_id or not out_track_id:
            return False
        if not carrier_ids:
            logger.warning("[card-reply-store] delivery has no successful carrier id")
            return False
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            now = time.time()
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO card_replies "
                    "(chat_id, carrier_id, out_track_id, content, updated_epoch) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (chat_id, carrier_id, out_track_id, str(content)[:20000], now)
                        for carrier_id in carrier_ids
                    ],
                )
                conn.execute(
                    "DELETE FROM card_replies WHERE rowid NOT IN "
                    "(SELECT rowid FROM card_replies "
                    "ORDER BY updated_epoch DESC, rowid DESC LIMIT ?)",
                    (self._max_rows,),
                )
            logger.info(
                "[card-reply-store] remembered has_chat_id=%s carrier_count=%d",
                bool(chat_id),
                len(carrier_ids),
            )
            return True
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "[card-reply-store] remember failed has_chat_id=%s carrier_count=%d",
                bool(chat_id),
                len(carrier_ids),
            )
            return False
        finally:
            if conn is not None:
                conn.close()

    def update_content(self, chat_id: str, out_track_id: str, content: str) -> bool:
        if not chat_id or not out_track_id:
            return False
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            with conn:
                cursor = conn.execute(
                    "UPDATE card_replies SET content = ?, updated_epoch = ? "
                    "WHERE chat_id = ? AND out_track_id = ?",
                    (str(content)[:20000], time.time(), chat_id, out_track_id),
                )
            return cursor.rowcount > 0
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "[card-reply-store] update failed has_chat_id=%s",
                bool(chat_id),
            )
            return False
        finally:
            if conn is not None:
                conn.close()

    def remember_webhook_delivery(
        self,
        chat_id: str,
        content: str,
        request_started_ms: int,
        request_finished_ms: int,
        response_body: Any = None,
    ) -> bool:
        """Persist the actual session-webhook output and its server-send window."""
        if not chat_id or not content:
            return False
        try:
            started_ms = int(request_started_ms)
            finished_ms = max(started_ms, int(request_finished_ms))
        except (TypeError, ValueError):
            return False
        message_ids = _webhook_response_message_ids(response_body) or [""]
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            now = time.time()
            with conn:
                conn.executemany(
                    "INSERT INTO webhook_replies "
                    "(chat_id, message_id, request_started_ms, request_finished_ms, "
                    "content, updated_epoch) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            chat_id,
                            message_id,
                            started_ms,
                            finished_ms,
                            str(content)[:20000],
                            now,
                        )
                        for message_id in message_ids
                    ],
                )
                conn.execute(
                    "DELETE FROM webhook_replies WHERE id NOT IN "
                    "(SELECT id FROM webhook_replies "
                    "ORDER BY updated_epoch DESC, id DESC LIMIT ?)",
                    (self._max_rows,),
                )
            logger.info(
                "[webhook-reply-store] remembered has_chat_id=%s exact_id_count=%d",
                bool(chat_id),
                0 if message_ids == [""] else len(message_ids),
            )
            return True
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "[webhook-reply-store] remember failed has_chat_id=%s",
                bool(chat_id),
            )
            return False
        finally:
            if conn is not None:
                conn.close()

    def _resolve_webhook(
        self,
        chat_id: str,
        message_id: str,
        created_at: Any,
    ) -> tuple[Optional[str], str, int]:
        if not chat_id:
            return None, "none", 0
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            if message_id:
                rows = conn.execute(
                    "SELECT content FROM webhook_replies "
                    "WHERE chat_id = ? AND message_id = ? "
                    "ORDER BY updated_epoch DESC, id DESC LIMIT 2",
                    (chat_id, message_id),
                ).fetchall()
                if rows:
                    return str(rows[0][0]), "message_id", len(rows)

            created_ms = _coerce_epoch_ms(created_at)
            if created_ms is None:
                return None, "none", 0
            exact_rows = conn.execute(
                "SELECT content FROM webhook_replies "
                "WHERE chat_id = ? "
                "AND request_started_ms <= ? "
                "AND request_finished_ms >= ? "
                "ORDER BY updated_epoch DESC, id DESC LIMIT 3",
                (chat_id, created_ms, created_ms),
            ).fetchall()
            if len(exact_rows) == 1:
                return str(exact_rows[0][0]), "created_at", 1
            if len(exact_rows) > 1:
                return None, "created_at", len(exact_rows)
            rows = conn.execute(
                "SELECT content FROM webhook_replies "
                "WHERE chat_id = ? "
                "AND request_started_ms <= ? "
                "AND request_finished_ms >= ? "
                "ORDER BY updated_epoch DESC, id DESC LIMIT 3",
                (
                    chat_id,
                    created_ms + _WEBHOOK_REPLY_MATCH_SLOP_MS,
                    created_ms - _WEBHOOK_REPLY_MATCH_SLOP_MS,
                ),
            ).fetchall()
            # With no platform message id, never guess between two sends whose
            # request windows overlap the same DingTalk createdAt timestamp.
            if len(rows) == 1:
                return str(rows[0][0]), "created_at", 1
            return None, "created_at", len(rows)
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "[webhook-reply-store] lookup failed has_chat_id=%s",
                bool(chat_id),
            )
            return None, "error", 0
        finally:
            if conn is not None:
                conn.close()

    def resolve(self, chat_id: str, carrier_id: str) -> Optional[str]:
        if not chat_id or not carrier_id:
            return None
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT content FROM card_replies "
                "WHERE chat_id = ? AND carrier_id = ?",
                (chat_id, carrier_id),
            ).fetchone()
            return str(row[0]) if row and row[0] else None
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "[card-reply-store] lookup failed has_chat_id=%s",
                bool(chat_id),
            )
            return None
        finally:
            if conn is not None:
                conn.close()

    def resolve_message(self, chat_id: str, message: "ChatbotMessage") -> Optional[str]:
        replied = _get_text_extensions(message).get("repliedMsg")
        if not isinstance(replied, dict):
            return None
        msg_type = str(replied.get("msgType") or replied.get("msgtype") or "").lower()
        if msg_type != "interactivecard":
            return None
        carrier_id = str(replied.get("msgId") or replied.get("msgid") or "").strip()
        original = self.resolve(chat_id, carrier_id)
        source = "carrier_id"
        candidate_count = 1 if original else 0
        if not original:
            original, source, candidate_count = self._resolve_webhook(
                chat_id,
                carrier_id,
                replied.get("createdAt") or replied.get("created_at"),
            )
        logger.info(
            "[card-reply-store] interactiveCard lookup=%s source=%s "
            "candidate_count=%d has_chat_id=%s",
            "hit" if original else "miss",
            source,
            candidate_count,
            bool(chat_id),
        )
        return original


def _get_replied_file_content(message: "ChatbotMessage") -> Optional[Dict[str, Any]]:
    replied = _get_text_extensions(message).get("repliedMsg")
    if not isinstance(replied, dict):
        return None
    msg_type = str(replied.get("msgType") or replied.get("msgtype") or "").lower()
    content = replied.get("content")
    if msg_type != "file" or not isinstance(content, dict):
        return None
    return content


def _extract_replied_text_original(replied: Dict[str, Any]) -> str:
    """Best-effort extraction of the quoted message's original text.

    DingTalk usually omits the original text for a text-reply (``repliedMsg`` carries
    only ``msgId``/``msgType``). When ``content`` is present it may be a plain string
    or a dict such as ``{"content": "..."}`` / ``{"text": "..."}``. Return "" when no
    usable text is found so the adapter can ask the user for clarification.
    """
    content = replied.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        for key in ("content", "text", "value"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def build_reply_kwargs(message: "ChatbotMessage") -> Dict[str, Any]:
    """Build best-effort MessageEvent reply context kwargs."""
    try:
        replied = (_get_text_extensions(message) or {}).get("repliedMsg") or {}
        if not isinstance(replied, dict) or not replied:
            return {}
        msg_id = str(replied.get("msgId") or replied.get("msgid") or "").strip()
        msg_type = str(replied.get("msgType") or replied.get("msgtype") or "").lower()
        if not msg_id or msg_type == "file":
            return {}
        original = _extract_replied_text_original(replied)
        # H1-20260729 埋点：`repliedMsg` 是钉钉未文档化的透传字段，官方文档无定义，
        # 项目内从无生产实测数据——"钉钉通常不给原文"一直只是一句无出处的注释。
        # 只记字段名与值的类型/长度，绝不记内容（隐私）。用于判断：不同 msgType
        # 下钉钉到底给了什么、有没有可用于精确定位原消息的元数据（如时间戳）。
        logger.info(
            "[reply-probe] repliedMsg keys=%s shapes=%s msgType=%s original_len=%d",
            _safe_keys(replied),
            {k: f"{type(v).__name__}/{len(v) if isinstance(v, (str, dict, list)) else 0}"
             for k, v in replied.items()},
            msg_type,
            len(original),
        )
        return {
            "reply_to_message_id": msg_id,
            "reply_to_text": original if original else _REPLY_ORIGINAL_UNAVAILABLE,
            "reply_to_is_own_message": False,
        }
    except Exception:
        return {}
