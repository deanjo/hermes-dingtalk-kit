"""DingTalk inbound reply and forwarded-message helpers."""

import json
import logging
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger(__name__)

_FORWARD_DIAG_MAX_ITEMS = 8
_DINGTALK_PLACEHOLDER_TEXTS = {"[图文消息]", "群聊的聊天记录"}

# T27: sentinel meaning "user replied to an earlier message but the platform did
# not deliver the original text". Value MUST match run.py's _REPLY_ORIGINAL_UNAVAILABLE
# exactly - run.py keys off it to inject a Chinese back-reference instruction.
_REPLY_ORIGINAL_UNAVAILABLE = "\x00__HERMES_REPLY_ORIGINAL_UNAVAILABLE__\x00"


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
    usable text is found so the caller can fall back to the sentinel.
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
        return {
            "reply_to_message_id": msg_id,
            "reply_to_text": original if original else _REPLY_ORIGINAL_UNAVAILABLE,
            "reply_to_is_own_message": False,
        }
    except Exception:
        return {}
