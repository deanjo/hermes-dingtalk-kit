"""DingTalk group mention and allowlist helpers."""

import json
import os
import re
from typing import Any, List, Set


def require_mention(extra: dict) -> bool:
    """Return whether group chats should require an explicit bot trigger."""
    configured = extra.get("require_mention")
    if configured is not None:
        if isinstance(configured, str):
            return configured.lower() in {"true", "1", "yes", "on"}
        return bool(configured)
    return os.getenv("DINGTALK_REQUIRE_MENTION", "false").lower() in {"true", "1", "yes", "on"}


def free_response_chats(extra: dict) -> Set[str]:
    raw = extra.get("free_response_chats")
    if raw is None:
        raw = os.getenv("DINGTALK_FREE_RESPONSE_CHATS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def allowed_chats(extra: dict) -> Set[str]:
    """Return the whitelist of group chat IDs the bot will respond in."""
    raw = extra.get("allowed_chats")
    if raw is None:
        raw = os.getenv("DINGTALK_ALLOWED_CHATS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def compile_mention_patterns(extra: dict, logger: Any, adapter_name: str) -> List[re.Pattern]:
    """Compile optional regex wake-word patterns for group triggers."""
    patterns = extra.get("mention_patterns")
    if patterns is None:
        raw = os.getenv("DINGTALK_MENTION_PATTERNS", "").strip()
        if raw:
            try:
                loaded = json.loads(raw)
            except Exception:
                loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                if not loaded:
                    loaded = [part.strip() for part in raw.split(",") if part.strip()]
            patterns = loaded

    if patterns is None:
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list):
        logger.warning(
            "[%s] dingtalk mention_patterns must be a list or string; got %s",
            adapter_name,
            type(patterns).__name__,
        )
        return []

    compiled: List[re.Pattern] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            logger.warning("[%s] Invalid DingTalk mention pattern %r: %s", adapter_name, pattern, exc)
    if compiled:
        logger.info("[%s] Loaded %d DingTalk mention pattern(s)", adapter_name, len(compiled))
    return compiled


def load_allowed_users(extra: dict) -> Set[str]:
    """Load allowed-users list from config.extra or env var."""
    raw = extra.get("allowed_users")
    if raw is None:
        raw = os.getenv("DINGTALK_ALLOWED_USERS", "")
    if isinstance(raw, list):
        items = [str(part).strip() for part in raw if str(part).strip()]
    else:
        items = [part.strip() for part in str(raw).split(",") if part.strip()]
    return {item.lower() for item in items}


def is_user_allowed(allowed_users: Set[str], sender_id: str, sender_staff_id: str) -> bool:
    if not allowed_users or "*" in allowed_users:
        return True
    candidates = {(sender_id or "").lower(), (sender_staff_id or "").lower()}
    candidates.discard("")
    return bool(candidates & allowed_users)


def message_mentions_bot(message: "ChatbotMessage") -> bool:
    return bool(getattr(message, "is_in_at_list", False))


def message_matches_patterns(patterns: List[re.Pattern], text: str) -> bool:
    if not text or not patterns:
        return False
    return any(pattern.search(text) for pattern in patterns)


def should_process_message(
    *,
    extra: dict,
    mention_patterns: List[re.Pattern],
    message: "ChatbotMessage",
    text: str,
    is_group: bool,
    chat_id: str,
) -> bool:
    """Apply DingTalk group trigger rules."""
    if not is_group:
        return True
    allowed = allowed_chats(extra)
    if allowed and chat_id and chat_id not in allowed:
        return False
    if chat_id and chat_id in free_response_chats(extra):
        return True
    if not require_mention(extra):
        return True
    if message_mentions_bot(message):
        return True
    return message_matches_patterns(mention_patterns, text)
