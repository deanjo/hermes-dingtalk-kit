"""DingTalk media extraction helpers."""

import mimetypes
from typing import Any

try:
    from .reply_context import _get_replied_file_content
except ImportError:
    from reply_context import _get_replied_file_content  # type: ignore


DINGTALK_TYPE_MAPPING = {
    "picture": "image",
    "voice": "audio",
}


def extract_media(message: "ChatbotMessage", message_type: Any):
    """Extract media info from message. Returns (MessageType, [urls], [mime_types])."""
    msg_type = message_type.TEXT
    media_urls = []
    media_types = []

    image_content = getattr(message, "image_content", None)
    if image_content:
        download_code = getattr(image_content, "download_code", None)
        if download_code:
            media_urls.append(download_code)
            media_types.append("image")
            msg_type = message_type.PHOTO

    rich_text = getattr(message, "rich_text_content", None) or getattr(
        message, "rich_text", None
    )
    if rich_text:
        rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
        if isinstance(rich_list, list):
            for item in rich_list:
                if isinstance(item, dict):
                    dl_code = (
                        item.get("downloadCode")
                        or item.get("pictureDownloadCode")
                        or item.get("download_code")
                        or ""
                    )
                    item_type = item.get("type", "")
                    if dl_code:
                        mapped = DINGTALK_TYPE_MAPPING.get(item_type, "file")
                        media_urls.append(dl_code)
                        if mapped == "image":
                            media_types.append("image")
                            if msg_type == message_type.TEXT:
                                msg_type = message_type.PHOTO
                        elif mapped == "audio":
                            media_types.append("audio")
                            if msg_type == message_type.TEXT:
                                if item_type == "voice":
                                    msg_type = message_type.VOICE
                                else:
                                    msg_type = message_type.AUDIO
                        elif mapped == "video":
                            media_types.append("video")
                            if msg_type == message_type.TEXT:
                                msg_type = message_type.VIDEO
                        else:
                            media_types.append("application/octet-stream")
                            if msg_type == message_type.TEXT:
                                msg_type = message_type.DOCUMENT

    replied_file = _get_replied_file_content(message)
    if replied_file:
        dl_code = (
            replied_file.get("downloadCode")
            or replied_file.get("download_code")
            or ""
        )
        if dl_code:
            filename = (
                replied_file.get("fileName")
                or replied_file.get("filename")
                or replied_file.get("name")
                or ""
            )
            guessed_type = mimetypes.guess_type(str(filename))[0] if filename else None
            media_urls.append(dl_code)
            media_types.append(guessed_type or "application/octet-stream")
            if msg_type == message_type.TEXT:
                msg_type = message_type.DOCUMENT

    msg_type_str = getattr(message, "message_type", "") or ""
    if msg_type_str == "picture" and not media_urls:
        msg_type = message_type.PHOTO
    elif msg_type_str == "richText":
        if msg_type == message_type.TEXT and any("image" in t for t in media_types):
            msg_type = message_type.PHOTO
    elif msg_type_str == "audio":
        if msg_type == message_type.TEXT:
            msg_type = message_type.VOICE
    elif msg_type_str in {"file", "image"}:
        extensions = getattr(message, "extensions", {}) or {}
        ext_content = extensions.get("content", {})
        if isinstance(ext_content, dict):
            dl_code = ext_content.get("downloadCode") or ""
            filename = ext_content.get("fileName") or ""
            if dl_code and dl_code not in media_urls:
                mime = mimetypes.guess_type(str(filename))[0] if filename else None
                mime = mime or "application/octet-stream"
                media_urls.append(dl_code)
                media_types.append(mime)
                if msg_type == message_type.TEXT:
                    msg_type = (
                        message_type.PHOTO
                        if msg_type_str == "image" or mime.startswith("image/")
                        else message_type.DOCUMENT
                    )

    return msg_type, media_urls, media_types
