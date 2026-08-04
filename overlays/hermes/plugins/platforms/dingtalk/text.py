"""DingTalk text extraction across supported SDK message shapes."""

from __future__ import annotations

import json


def extract_text(message: "ChatbotMessage") -> str:
    """Extract user-visible text without leaking SDK object reprs."""
    text = getattr(message, "text", None) or ""
    if hasattr(text, "content"):
        content = (text.content or "").strip()
    elif isinstance(text, dict):
        content = text.get("content", "").strip()
    else:
        content = str(text).strip()

    if not content:
        rich_text = getattr(message, "rich_text_content", None) or getattr(
            message, "rich_text", None
        )
        if rich_text:
            rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
            if isinstance(rich_list, list):
                parts = []
                for item in rich_list:
                    if isinstance(item, dict):
                        value = item.get("text") or item.get("content") or ""
                    else:
                        value = getattr(item, "text", "") or ""
                    if value:
                        parts.append(value)
                content = " ".join(parts).strip()

    msg_type = getattr(message, "message_type", "") or ""
    extensions = getattr(message, "extensions", {}) or {}
    if not content and msg_type == "audio":
        audio = extensions.get("content", {})
        if isinstance(audio, dict):
            content = str(audio.get("recognition") or "").strip()

    if not content and msg_type == "file":
        file_content = extensions.get("content", {})
        if isinstance(file_content, dict) and file_content.get("fileName"):
            content = f"[文件] {file_content['fileName']}"

    if not content and msg_type == "card":
        card = extensions.get("card", {})
        if isinstance(card, dict):
            title = str(card.get("title") or "").strip()
            raw_content = card.get("content")
            document_url = ""
            if isinstance(raw_content, dict):
                document_url = str(
                    raw_content.get("url") or raw_content.get("docUrl") or ""
                ).strip()
            elif isinstance(raw_content, str) and raw_content.strip():
                try:
                    parsed = json.loads(raw_content)
                except (TypeError, ValueError):
                    document_url = raw_content.strip()
                else:
                    if isinstance(parsed, dict):
                        document_url = str(
                            parsed.get("url") or parsed.get("docUrl") or ""
                        ).strip()
            parts = [f"[文档] {title}"] if title else []
            if document_url:
                parts.append(document_url)
            content = " ".join(parts)
        if not content:
            ext_text = extensions.get("text", {})
            if isinstance(ext_text, dict):
                content = str(ext_text.get("content") or "").strip()

    if not content and msg_type == "interactiveCard":
        card = extensions.get("content", {})
        if isinstance(card, dict):
            title = str(card.get("title") or "").strip()
            document_url = str(card.get("biz_custom_action_url") or "").strip()
            if title or document_url:
                parts = [f"[文档卡片] {title}" if title else "[文档卡片]"]
                if document_url:
                    parts.append(document_url)
                content = " ".join(parts)

    return content
