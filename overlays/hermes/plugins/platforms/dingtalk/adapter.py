"""
DingTalk platform adapter using Stream Mode.

Uses dingtalk-stream SDK (>=0.20) for real-time message reception without webhooks.
Responses are sent via DingTalk's session webhook (markdown format).
Supports: text, images, audio, video, rich text, files, and group @mentions.

Requires:
    pip install "dingtalk-stream>=0.20" httpx
    DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET env vars

Configuration in config.yaml:
    platforms:
      dingtalk:
        enabled: true
        # Optional group-chat gating (mirrors Slack/Telegram/Discord):
        require_mention: true            # or DINGTALK_REQUIRE_MENTION env var
        # free_response_chats:           # conversations that skip require_mention
        #   - cidABC==
        # mention_patterns:              # regex wake-words (e.g. Chinese bot names)
        #   - "^小马"
        # allowed_users:                 # staff_id or sender_id list; "*" = any
        #   - "manager1234"
        extra:
          client_id: "your-app-key"      # or DINGTALK_CLIENT_ID env var
          client_secret: "your-secret"   # or DINGTALK_CLIENT_SECRET env var
"""

import asyncio
import logging
import mimetypes
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
    from dingtalk_stream.frames import CallbackMessage, AckMessage

    DINGTALK_STREAM_AVAILABLE = True
except Exception:  # noqa: BLE001 — broad: optional SDK's transitive deps (cryptography) may raise non-ImportError; degrade gracefully (#41112)
    DINGTALK_STREAM_AVAILABLE = False
    dingtalk_stream = None  # type: ignore[assignment]
    ChatbotMessage = None  # type: ignore[assignment]
    CallbackMessage = None  # type: ignore[assignment]
    AckMessage = type(
        "AckMessage",
        (),
        {
            "STATUS_OK": 200,
            "STATUS_SYSTEM_EXCEPTION": 500,
        },
    )  # type: ignore[assignment]

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

# Card SDK for AI Cards (following QwenPaw pattern).
# Catch broad Exception, not just ImportError: the alibabacloud_dingtalk SDK
# transitively imports cryptography and can raise AttributeError (not
# ImportError) when the installed cryptography version skews from what the SDK
# expects (e.g. `cryptography.utils.DeprecatedIn46` missing on older
# cryptography). An optional SDK with a broken dependency chain must degrade
# gracefully — same as a missing one — rather than crash the whole adapter
# (and therefore the whole plugin) import. #41112.
try:
    from alibabacloud_dingtalk.card_1_0 import (
        client as dingtalk_card_client,
        models as dingtalk_card_models,
    )
    from alibabacloud_dingtalk.robot_1_0 import (
        client as dingtalk_robot_client,
        models as dingtalk_robot_models,
    )
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as tea_util_models

    CARD_SDK_AVAILABLE = True
except Exception:
    CARD_SDK_AVAILABLE = False
    dingtalk_card_client = None
    dingtalk_card_models = None
    dingtalk_robot_client = None
    dingtalk_robot_models = None
    open_api_models = None
    tea_util_models = None

from .delivery_gate import blocked_send_result  # H1 治理第 5 项 · 出站闸门
from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_media_bytes,
)
from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Read a profile-scoped secret, with the default profile env fallback."""
    try:
        value = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        value = os.getenv(name)
    return value if value is not None else default

try:
    from .incoming import make_incoming_handler
    from .markdown import normalize_markdown
    from .media import extract_media
    from .mentions import compile_mention_patterns, is_user_allowed, load_allowed_users, mention_meta_line, should_process_message, stamp_group_text
    from .text import extract_text
    from .plugin_setup import _apply_yaml_config, _is_connected, _standalone_send, interactive_setup
    from .reply_context import (
        CardReplyStore,
        _forwarded_chat_text_from_raw,
        _get_replied_file_content,
        _is_placeholder_text,
        _log_forward_diag,
        append_full_reply_text, build_reply_kwargs, append_conversation_context,
    )
    from .task_binding import resolve_gateway_profile, resolve_task_binding
    from .task_binding import restore_h1_binding, set_h1_dispatch_scope, mark_h1_turn_delivered, h1_turn_meta_lines, is_h1_failure_receipt
except ImportError:
    import sys
    from pathlib import Path
    _MODULE_DIR = str(Path(__file__).resolve().parent)
    if _MODULE_DIR not in sys.path: sys.path.insert(0, _MODULE_DIR)
    from incoming import make_incoming_handler  # type: ignore
    from markdown import normalize_markdown  # type: ignore
    from media import extract_media  # type: ignore
    from mentions import compile_mention_patterns, is_user_allowed, load_allowed_users, mention_meta_line, should_process_message, stamp_group_text  # type: ignore
    from text import extract_text  # type: ignore
    from plugin_setup import _apply_yaml_config, _is_connected, _standalone_send, interactive_setup  # type: ignore
    from reply_context import (  # type: ignore
        CardReplyStore,
        _forwarded_chat_text_from_raw,
        _get_replied_file_content,
        _is_placeholder_text,
        _log_forward_diag,
        append_full_reply_text, build_reply_kwargs, append_conversation_context,
    )
    from task_binding import resolve_gateway_profile, resolve_task_binding  # type: ignore
    from task_binding import restore_h1_binding, set_h1_dispatch_scope, mark_h1_turn_delivered, h1_turn_meta_lines, is_h1_failure_receipt  # type: ignore

logger = logging.getLogger(__name__)

# Keep the official module-level handler entry available for Core callers and
# tests.  connect() still builds a fresh class after optional lazy dependency
# loading, so this compatibility alias does not interfere with that path.
_IncomingHandler = make_incoming_handler(
    dingtalk_stream=dingtalk_stream,
    dingtalk_stream_available=DINGTALK_STREAM_AVAILABLE,
    chatbot_message_cls=ChatbotMessage,
    ack_message_cls=AckMessage,
    logger=logger,
    log_forward_diag=_log_forward_diag,
)


MAX_MESSAGE_LENGTH = 20000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
_SESSION_WEBHOOKS_MAX = 500
_DINGTALK_WEBHOOK_RE = re.compile(r'^https://(?:api|oapi)\.dingtalk\.com/')
_TASK_BINDING_CLARIFICATION = "我没有找到有效的任务绑定。请用“#任务 board/task 你的问题”重试，例如：#任务 agong/t_deadbeef 联系人为什么没显示。"

def dingtalk_deps_present() -> bool:
    """PASSIVE probe: are dingtalk-stream/httpx importable right now?"""
    return DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE


def ensure_dingtalk_deps() -> bool:
    """Install optional DingTalk dependencies without reading credentials."""
    global DINGTALK_STREAM_AVAILABLE, dingtalk_stream, ChatbotMessage, CallbackMessage, AckMessage
    global HTTPX_AVAILABLE, httpx
    if DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.dingtalk", prompt=False)
    except Exception:
        return False
    try:
        import dingtalk_stream as _ds
        from dingtalk_stream import ChatbotMessage as _CM
        from dingtalk_stream.frames import CallbackMessage as _CBM, AckMessage as _AM
        import httpx as _httpx
    except Exception:
        return False
    dingtalk_stream = _ds
    ChatbotMessage = _CM
    CallbackMessage = _CBM
    AckMessage = _AM
    httpx = _httpx
    DINGTALK_STREAM_AVAILABLE = True
    HTTPX_AVAILABLE = True
    return True


def check_dingtalk_requirements() -> bool:
    """Return whether dependencies and DingTalk credentials are available."""
    return bool(ensure_dingtalk_deps() and os.getenv("DINGTALK_CLIENT_ID") and
                _get_scoped_secret("DINGTALK_CLIENT_SECRET"))

class DingTalkAdapter(BasePlatformAdapter):
    """Receive Stream callbacks; send replies through session webhooks or AI Cards."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    @property
    def SUPPORTS_MESSAGE_EDITING(self) -> bool:  # noqa: N802
        """Enable gateway editing only when AI Cards are actually available."""
        return bool(self._card_template_id and self._card_sdk)

    @property
    def REQUIRES_EDIT_FINALIZE(self) -> bool:  # noqa: N802
        """AI Card lifecycle requires an explicit ``finalize=True`` edit
        to close the streaming indicator, even when the final content is
        identical to the last streamed update.  Enabled only when cards
        are configured — webhook-only DingTalk doesn't need it.
        """
        return bool(self._card_template_id and self._card_sdk)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DINGTALK)

        extra = config.extra or {}
        self._client_id: str = extra.get("client_id") or os.getenv(
            "DINGTALK_CLIENT_ID", ""
        )
        self._client_secret: str = extra.get("client_secret") or _get_scoped_secret(
            "DINGTALK_CLIENT_SECRET", ""
        )

        # Group-chat gating (mirrors Slack/Telegram/Discord/WhatsApp conventions).
        # Mention state is the structured ``is_in_at_list`` attribute from the
        # dingtalk-stream SDK (set from the callback's ``isInAtList`` flag),
        # not text parsing.
        self._mention_patterns: List[re.Pattern] = compile_mention_patterns(extra, logger, self.name)
        self._allowed_users: Set[str] = load_allowed_users(extra)

        self._stream_client: Any = None
        self._stream_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._card_sdk: Optional[Any] = None
        self._robot_sdk: Optional[Any] = None
        self._robot_code: str = extra.get("robot_code") or self._client_id

        # Message deduplication
        self._dedup = MessageDeduplicator(max_size=1000)
        # Map chat_id -> (session_webhook, expired_time_ms) for reply routing
        self._session_webhooks: Dict[str, tuple[str, int]] = {}
        # Map chat_id -> last inbound ChatbotMessage. Keyed by chat_id instead
        # of a single class attribute to avoid cross-message clobbering when
        # multiple conversations run concurrently.
        self._message_contexts: Dict[str, Any] = {}
        self._card_template_id: Optional[str] = extra.get("card_template_id")

        # Chats for which we've already fired the Done reaction — prevents
        # double-firing across segment boundaries or parallel flows
        # (tool-progress + stream-consumer both finalizing their cards).
        # Reset each inbound message.
        self._done_emoji_fired: Set[str] = set()
        # Cards in streaming state per chat: chat_id -> { out_track_id -> last_content }.
        # Every `send()` creates+finalizes a card (closed state).  A subsequent
        # `edit_message(finalize=False)` re-opens the card (DingTalk's API
        # allows streaming_update on a finalized card — it flips back to
        # streaming).  We track those reopened cards so the next `send()` can
        # auto-close them as siblings — otherwise tool-progress cards get
        # stuck in streaming state forever.
        self._streaming_cards: Dict[str, Dict[str, str]] = {}
        self._card_reply_store = CardReplyStore()
        # Track fire-and-forget emoji/reaction coroutines so Python's GC
        # doesn't drop them mid-flight, and we can cancel them on disconnect.
        self._bg_tasks: Set[asyncio.Task] = set()
        self._gateway_profile: Optional[str] = resolve_gateway_profile()  # owning multiplex profile

    # -- Official adapter compatibility surface ---------------------------

    def _is_user_allowed(self, sender_id: str, sender_staff_id: str) -> bool:
        return is_user_allowed(self._allowed_users, sender_id, sender_staff_id)

    def _message_matches_mention_patterns(self, text: str) -> bool:
        return bool(text) and any(pattern.search(text) for pattern in self._mention_patterns)

    def _should_process_message(
        self,
        message: "ChatbotMessage",
        text: str,
        is_group: bool,
        chat_id: str,
    ) -> bool:
        return should_process_message(
            extra=self.config.extra or {},
            mention_patterns=self._mention_patterns,
            message=message,
            text=text,
            is_group=is_group,
            chat_id=chat_id,
        )

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to DingTalk via Stream Mode."""
        if not DINGTALK_STREAM_AVAILABLE:
            logger.warning(
                "[%s] dingtalk-stream not installed. Run: pip install 'dingtalk-stream>=0.20'",
                self.name,
            )
            return False
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed. Run: pip install httpx", self.name)
            return False
        if not self._client_id or not self._client_secret:
            logger.warning("[%s] DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required", self.name)
            return False

        try:
            # Tighter keepalive so idle CLOSE_WAIT drains promptly (#18451).
            from gateway.platforms._http_client_limits import platform_httpx_limits
            self._http_client = httpx.AsyncClient(
                timeout=30.0, limits=platform_httpx_limits(),
            )

            credential = dingtalk_stream.Credential(
                self._client_id, self._client_secret
            )
            self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)

            # Initialize card SDK if available and configured
            if CARD_SDK_AVAILABLE and self._card_template_id:
                sdk_config = open_api_models.Config()
                sdk_config.protocol = "https"
                sdk_config.region_id = "central"
                self._card_sdk = dingtalk_card_client.Client(sdk_config)
                self._robot_sdk = dingtalk_robot_client.Client(sdk_config)
                logger.info(
                    "[%s] Card SDK initialized with template: %s",
                    self.name,
                    self._card_template_id,
                )
            elif CARD_SDK_AVAILABLE:
                # Initialize robot SDK even without card template (for media download)
                sdk_config = open_api_models.Config()
                sdk_config.protocol = "https"
                sdk_config.region_id = "central"
                self._robot_sdk = dingtalk_robot_client.Client(sdk_config)
                logger.info("[%s] Robot SDK initialized (media download)", self.name)

            # Capture the current event loop for cross-thread dispatch
            loop = asyncio.get_running_loop()
            handler_cls = make_incoming_handler(
                dingtalk_stream=dingtalk_stream,
                dingtalk_stream_available=DINGTALK_STREAM_AVAILABLE,
                chatbot_message_cls=ChatbotMessage,
                ack_message_cls=AckMessage,
                logger=logger,
                log_forward_diag=_log_forward_diag,
            )
            handler = handler_cls(self, loop)
            self._stream_client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC, handler
            )

            self._stream_task = asyncio.create_task(self._run_stream())
            self._mark_connected()
            logger.info("[%s] Connected via Stream Mode", self.name)
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """Run the async stream client with auto-reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                logger.debug("[%s] Starting stream client...", self.name)
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Stream client error: %s", self.name, e)

            if not self._running:
                return

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def disconnect(self) -> None:
        """Disconnect from DingTalk."""
        self._running = False
        self._mark_disconnected()

        # Close the active websocket first so the stream task sees the
        # disconnection and exits cleanly, rather than getting stuck
        # awaiting frames that will never arrive.
        websocket = getattr(self._stream_client, "websocket", None) if self._stream_client else None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as e:
                logger.debug("[%s] websocket close during disconnect failed: %s", self.name, e)

        if self._stream_task:
            # Try graceful close first if SDK supports it. The SDK's close()
            # is sync and may block on network I/O, so offload to a thread.
            if hasattr(self._stream_client, "close"):
                try:
                    await asyncio.to_thread(self._stream_client.close)
                except Exception:
                    pass

            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug("[%s] stream task did not exit cleanly during disconnect", self.name)
            self._stream_task = None

        # Cancel any in-flight background tasks (emoji reactions, etc.)
        if self._bg_tasks:
            for task in list(self._bg_tasks):
                task.cancel()
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        # Finalize any open streaming cards before the HTTP client closes so
        # they don't stay stuck in streaming state on DingTalk's UI after
        # a gateway restart.  _close_streaming_siblings handles its own
        # per-card exceptions; the outer try is a safety net for token fetch.
        for _chat_id in list(self._streaming_cards):
            try:
                await self._close_streaming_siblings(_chat_id)
            except Exception as _exc:
                logger.debug(
                    "[%s] Failed to finalize streaming card on disconnect for %s: %s",
                    self.name, _chat_id, _exc,
                )

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._stream_client = None
        self._session_webhooks.clear()
        self._message_contexts.clear()
        self._streaming_cards.clear()
        self._done_emoji_fired.clear()
        self._dedup.clear()
        logger.info("[%s] Disconnected", self.name)

    def _spawn_bg(self, coro) -> None:
        """Start a fire-and-forget coroutine and track it for cleanup."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # -- AI Card lifecycle helpers ------------------------------------------

    async def _close_streaming_siblings(self, chat_id: str) -> None:
        """Finalize any previously-open streaming cards for this chat.

        Called at the start of every ``send()`` so lingering tool-progress
        cards that were reopened by ``edit_message(finalize=False)`` get
        cleanly closed before the next card is created.  Without this,
        tool-progress cards stay stuck in streaming state after the agent
        moves on (there is no explicit "turn end" signal from the gateway).
        """
        cards = self._streaming_cards.pop(chat_id, None)
        if not cards:
            return
        token = await self._get_access_token()
        if not token:
            return
        for out_track_id, last_content in list(cards.items()):
            try:
                await self._stream_card_content(
                    out_track_id, token, last_content, finalize=True,
                )
                logger.debug(
                    "[%s] AI Card sibling closed: %s",
                    self.name, out_track_id,
                )
            except Exception as e:
                logger.debug(
                    "[%s] Sibling close failed for %s: %s",
                    self.name, out_track_id, e,
                )

    def _fire_done_reaction(self, chat_id: str) -> None:
        """Swap 🤔Thinking → 🥳Done (idempotent per chat).  B4: never fire
        Done on a turn whose final reply is a stamped failure receipt."""
        if chat_id in self._done_emoji_fired or is_h1_failure_receipt():
            return
        self._done_emoji_fired.add(chat_id)
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return

        async def _swap() -> None:
            await self._send_emotion(
                msg_id, conversation_id, "🤔Thinking", recall=True,
            )
            await self._send_emotion(
                msg_id, conversation_id, "🥳Done", recall=False,
            )

        self._spawn_bg(_swap())

    # -- Inbound message processing -----------------------------------------

    async def _on_message(
        self,
        message: "ChatbotMessage",
    ) -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return

        # Chat context
        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""
        # Anonymous senders must never share a pending task or confirmation.
        has_stable_sender = bool((sender_id or "").strip() or (sender_staff_id or "").strip())

        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"

        # Allowed-users gate (applies to both DM and group)
        if not is_user_allowed(self._allowed_users, sender_id, sender_staff_id):
            logger.debug(
                "[%s] Dropping message from non-allowlisted user has_staff_id=%s has_sender_id=%s",
                self.name, bool(sender_staff_id), bool(sender_id),
            )
            return

        forwarded_chat_text = _forwarded_chat_text_from_raw(
            getattr(message, "_hermes_raw_data", None)
        )
        forwarded_chat_gate_text = _forwarded_chat_text_from_raw(
            getattr(message, "_hermes_raw_data", None),
            include_senders=False,
        )

        # Apply the group mention/pattern gate before processing; DMs pass.
        _early_text = self._extract_text(message) or ""
        gate_text = (
            forwarded_chat_gate_text
            if forwarded_chat_gate_text and (not _early_text or _is_placeholder_text(_early_text))
            else f"{_early_text}\n{forwarded_chat_gate_text}".strip()
            if forwarded_chat_gate_text
            else _early_text
        )
        if not should_process_message(
            extra=self.config.extra or {},
            mention_patterns=self._mention_patterns,
            message=message,
            text=gate_text,
            is_group=is_group,
            chat_id=chat_id,
        ):
            logger.debug(
                "[%s] Dropping group message that failed mention gate has_message_id=%s has_chat_id=%s",
                self.name, bool(msg_id), bool(chat_id),
            )
            return

        # Keep incoming context and the Thinking→Done cycle scoped to this chat.
        if chat_id:
            self._message_contexts[chat_id] = message
            self._done_emoji_fired.discard(chat_id)

        # Store session webhook
        session_webhook = getattr(message, "session_webhook", None) or ""
        session_webhook_expired_time = (
            getattr(message, "session_webhook_expired_time", 0) or 0
        )
        if session_webhook and chat_id and _DINGTALK_WEBHOOK_RE.match(session_webhook):
            if len(self._session_webhooks) >= _SESSION_WEBHOOKS_MAX:
                try:
                    self._session_webhooks.pop(next(iter(self._session_webhooks)))
                except StopIteration:
                    pass
            self._session_webhooks[chat_id] = (
                session_webhook,
                session_webhook_expired_time,
            )

        # Resolve media download codes to URLs so vision tools can use them
        await self._resolve_media_codes(message)

        # Extract text content
        text = self._extract_text(message)
        if forwarded_chat_text:
            text = (
                forwarded_chat_text
                if not text or _is_placeholder_text(text)
                else f"{text}\n\n{forwarded_chat_text}"
            )

        # Determine message type and build media list
        msg_type, media_urls, media_types = extract_media(message, MessageType)
        _log_forward_diag(
            "after_extract",
            {
                "msgtype": getattr(message, "message_type", "") or "",
                "text": {"content": text or ""},
                "content": {"summary": "", "chatRecord": []},
                "media": {"count": len(media_urls), "types": list(media_types)},
            },
        )

        if not text and not media_urls:
            logger.debug("[%s] Empty message, skipping", self.name)
            return

        # Only explicit slash commands may bypass model interpretation.
        allow_gateway_control = (text or "").lstrip().startswith("/")
        current_text = text
        reply_kwargs = build_reply_kwargs(message)
        context = self._card_reply_store.prepare_reply(chat_id, message, reply_kwargs)
        self._card_reply_store.remember_incoming(chat_id, msg_id, text, message, media_urls, media_types)
        text = append_conversation_context(text, context)
        task_binding = None
        if (text or "").lstrip().startswith("#任务"):
            parsed_task_message = await resolve_task_binding(
                self, text or "", chat_id=chat_id, message_id=msg_id,
                clarification=_TASK_BINDING_CLARIFICATION,
            )
            if parsed_task_message is None:
                return
            task_binding, text = parsed_task_message.binding, parsed_task_message.message_text
        source = self.build_source(
            chat_id=chat_id,
            chat_name=getattr(message, "conversation_title", None),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_nick,
            user_id_alt=sender_staff_id if sender_staff_id else None,
            message_id=msg_id,
        )
        if task_binding:  # build_source() rejects the Kanban kwargs — stamp directly.
            source.board_slug, source.task_id = task_binding.board_slug, task_binding.task_id
        # Natural task intake is opt-in; preserve plain messages when disabled.
        if (self.config.extra or {}).get("natural_task_intake") is True:
            # Restore binding and metadata only for a stable sender.
            if not has_stable_sender:
                logger.warning("[%s] Message skipped: no stable sender identity", self.name)
                return
            set_h1_dispatch_scope(source=source, text=text, message_id=msg_id)
            if task_binding is None and not (text or "").lstrip().startswith("/"):
                source = await restore_h1_binding(self, source)
                meta_lines = h1_turn_meta_lines(
                    self, chat_id=chat_id, sender_user=sender_id or sender_staff_id, msg_id=msg_id
                )
                if meta_lines:
                    text = f"{text}\n\n" + "\n".join(meta_lines) if text else "\n".join(meta_lines)
        if is_group and not (text or "").lstrip().startswith("/"):
            text = stamp_group_text(text, sender_nick, mention_meta_line(message, self.config.extra or {}))
        create_at = getattr(message, "create_at", None)
        try:
            timestamp = (
                datetime.fromtimestamp(int(create_at) / 1000, tz=timezone.utc)
                if create_at
                else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)
        text = append_full_reply_text(text, reply_kwargs)
        text = self._card_reply_store.fit_model_context(text, reply_kwargs, current_text)
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=msg_id,
            raw_message=message,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            allow_gateway_control=allow_gateway_control,
            **reply_kwargs,
        )

        logger.debug(
            "[%s] Message accepted chat_type=%s text_len=%d media_count=%d",
            self.name,
            chat_type,
            len(text or ""),
            len(media_urls),
        )
        await self.handle_message(event)

    @staticmethod
    def _extract_text(message: "ChatbotMessage") -> str:
        return extract_text(message)

    def _extract_media(self, message: "ChatbotMessage"):
        return extract_media(message, MessageType)

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        return normalize_markdown(text)

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a markdown reply via DingTalk session webhook."""
        metadata = metadata or {}

        # H1 治理第 5 项 · 出站闸门：框架产物不进业务群（判据见 delivery_gate）。
        _blocked = blocked_send_result(metadata, reply_to, self.name, SendResult, logger)
        if _blocked is not None: return _blocked
        logger.debug("[%s] send() has_chat_id=%s card_enabled=%s", self.name,
                     bool(chat_id), bool(self._card_template_id and self._card_sdk))

        # Check metadata first (for direct webhook sends)
        session_webhook = metadata.get("session_webhook")
        if not session_webhook:
            webhook_info = self._get_valid_webhook(chat_id)
            if not webhook_info:
                logger.warning(
                    "[%s] No valid session_webhook has_chat_id=%s",
                    self.name, bool(chat_id),
                )
                return SendResult(
                    success=False,
                    error="No valid session_webhook available. Reply must follow an incoming message.",
                    raw_response={"delivery_outcome": "rejected"},
                )
            session_webhook, _ = webhook_info

        if not self._http_client:
            return SendResult(
                success=False,
                error="HTTP client not initialized",
                raw_response={"delivery_outcome": "rejected"},
            )

        # Look up the inbound message for this chat (for AI Card routing)
        current_message = self._message_contexts.get(chat_id)

        # ``reply_to`` is the signal that this send is the FINAL response to
        # an inbound user message — only `base.py:_send_with_retry` sets it.
        # Decisions keyed on it: finalize-on-create + fire Done only for
        # final replies; intermediate sends stay in streaming state.
        is_final_reply = reply_to is not None

        # Structured @-mentions require webhook markdown; AI Cards have no at field.
        raw_at = metadata.get("at_user_ids") or []
        if isinstance(raw_at, str):
            raw_at = [raw_at]
        at_user_ids = [str(u) for u in raw_at if str(u).strip()]

        # Try AI Card first (using alibabacloud_dingtalk.card_1_0 SDK).
        if (self._card_template_id and current_message and self._card_sdk
                and not at_user_ids and not metadata.get("reply_recovery_prompt")):
            # Close this chat's previous streaming cards before creating another.
            await self._close_streaming_siblings(chat_id)

            result = await self._create_and_stream_card(
                chat_id, current_message, content,
                finalize=is_final_reply,
            )
            if result and result.success:
                raw_response = (
                    dict(result.raw_response)
                    if isinstance(result.raw_response, dict)
                    else {}
                )
                raw_response["delivery_outcome"] = "delivered"
                result.raw_response = raw_response
                if is_final_reply:
                    # Final reply: card closed, swap Thinking → Done.
                    self._fire_done_reaction(chat_id)
                    mark_h1_turn_delivered(self, chat_id=chat_id, message_id=result.message_id)
                else:
                    # Keep intermediate cards open until another send or final edit.
                    self._streaming_cards.setdefault(chat_id, {})[
                        result.message_id
                    ] = content
                return result

            logger.warning("[%s] AI Card send failed, falling back to webhook", self.name)

        logger.debug("[%s] Sending via webhook", self.name)
        # Normalize markdown for DingTalk
        normalized = self._normalize_markdown(content)

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Hermes", "text": normalized},
        }
        if at_user_ids:
            payload["at"] = {"atUserIds": at_user_ids, "isAtAll": False}

        try:
            webhook_started_ms = time.time_ns() // 1_000_000
            resp = await self._http_client.post(
                session_webhook, json=payload, timeout=15.0
            )
            webhook_finished_ms = time.time_ns() // 1_000_000
            if resp.status_code < 300:
                # HTTP 200 with errcode != 0 is still a rejected delivery.
                try:
                    body_json = resp.json()
                except Exception:
                    body_json = None
                if isinstance(body_json, dict) and body_json.get("errcode", 0) != 0:
                    logger.warning(
                        "[%s] Send rejected by DingTalk errcode=%s errmsg=%s",
                        self.name, body_json.get("errcode"),
                        str(body_json.get("errmsg"))[:200],
                    )
                    return SendResult(
                        success=False,
                        error=f"DingTalk errcode {body_json.get('errcode')}:"
                              f" {str(body_json.get('errmsg'))[:200]}",
                        raw_response={"delivery_outcome": "rejected"},
                    )
                reply_context_saved = (
                    self._card_reply_store.remember_webhook_delivery(
                        chat_id, normalized, webhook_started_ms, webhook_finished_ms, body_json,
                    )
                )
                if reply_context_saved is False:
                    logger.warning("[webhook-reply-store] delivered but original was not saved")
                # Mark delivery and fire Done only for final replies.
                _webhook_out_id = uuid.uuid4().hex[:12]
                if is_final_reply:
                    self._fire_done_reaction(chat_id)
                    mark_h1_turn_delivered(self, chat_id=chat_id, message_id=_webhook_out_id)
                return SendResult(
                    success=True,
                    message_id=_webhook_out_id,
                    raw_response={"delivery_outcome": "delivered", "reply_context_saved": reply_context_saved},
                )
            body = resp.text
            logger.warning(
                "[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body[:200]
            )
            return SendResult(
                success=False,
                error=f"HTTP {resp.status_code}: {body[:200]}",
                raw_response={"delivery_outcome": "rejected"},
            )
        except httpx.TimeoutException:
            return SendResult(
                success=False,
                error="Timeout sending message to DingTalk",
                raw_response={"delivery_outcome": "unknown"},
            )
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(
                success=False,
                error=str(e),
                raw_response={"delivery_outcome": "unknown"},
            )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """DingTalk does not support typing indicators."""
        pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image via DingTalk markdown.

        DingTalk's session webhook only supports text/markdown payloads, not
        native image/file attachments. For remote image URLs, render the image
        inline with markdown so the user still sees the image. Local files need
        OpenAPI media upload and are handled separately.
        """
        image_block = f"![image]({image_url})"
        content = f"{caption}\n\n{image_block}" if caption else image_block
        return await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """DingTalk webhook replies cannot send local image files directly."""
        return SendResult(
            success=False,
            error=(
                "DingTalk session webhook replies do not support local image uploads. "
                "Only markdown/text replies are supported without OpenAPI media upload."
            ),
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """DingTalk webhook replies cannot send local file attachments directly."""
        return SendResult(
            success=False,
            error=(
                "DingTalk session webhook replies do not support local file attachments. "
                "Only markdown/text replies are supported without OpenAPI message send."
            ),
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a DingTalk conversation."""
        return {
            "name": chat_id,
            "type": "group" if "group" in chat_id.lower() else "dm",
        }

    def _get_valid_webhook(self, chat_id: str) -> Optional[tuple[str, int]]:
        """Get a valid (non-expired) session webhook for the given chat_id."""
        info = self._session_webhooks.get(chat_id)
        if not info:
            return None
        webhook, expired_time_ms = info
        # Check expiry with 5-minute safety margin
        if expired_time_ms and expired_time_ms > 0:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            safety_margin_ms = 5 * 60 * 1000
            if now_ms + safety_margin_ms >= expired_time_ms:
                # Expired, remove from cache
                self._session_webhooks.pop(chat_id, None)
                return None
        return info

    async def _create_and_stream_card(
        self,
        chat_id: str,
        message: Any,
        content: str,
        *,
        finalize: bool = True,
    ) -> Optional[SendResult]:
        """Create an AI Card, deliver it to the conversation, and stream initial content.

        Always called with ``finalize=True`` from ``send()`` (closed state).
        If the caller later issues ``edit_message(finalize=False)``, the
        DingTalk streaming_update API reopens the card into streaming
        state, and we track that in ``_streaming_cards`` for sibling
        cleanup on the next send.
        """
        try:
            token = await self._get_access_token()
            if not token:
                return None

            out_track_id = f"hermes_{uuid.uuid4().hex[:12]}"

            conversation_id = getattr(message, "conversation_id", "") or ""
            conversation_type = getattr(message, "conversation_type", "1")
            is_group = str(conversation_type) == "2"
            sender_staff_id = getattr(message, "sender_staff_id", "") or ""

            runtime = tea_util_models.RuntimeOptions()

            # Step 1: Create card with STREAM callback type
            create_request = dingtalk_card_models.CreateCardRequest(
                card_template_id=self._card_template_id,
                out_track_id=out_track_id,
                card_data=dingtalk_card_models.CreateCardRequestCardData(
                    card_param_map={"content": ""},
                ),
                callback_type="STREAM",
                im_group_open_space_model=(
                    dingtalk_card_models.CreateCardRequestImGroupOpenSpaceModel(
                        support_forward=True,
                    )
                ),
                im_robot_open_space_model=(
                    dingtalk_card_models.CreateCardRequestImRobotOpenSpaceModel(
                        support_forward=True,
                    )
                ),
            )

            create_headers = dingtalk_card_models.CreateCardHeaders(
                x_acs_dingtalk_access_token=token,
            )

            await self._card_sdk.create_card_with_options_async(
                create_request, create_headers, runtime
            )

            # Step 2: Deliver card to the conversation
            if is_group:
                open_space_id = f"dtv1.card//IM_GROUP.{conversation_id}"
                deliver_request = dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=open_space_id,
                    im_group_open_deliver_model=(
                        dingtalk_card_models.DeliverCardRequestImGroupOpenDeliverModel(
                            robot_code=self._robot_code,
                        )
                    ),
                )
            else:
                if not sender_staff_id:
                    logger.warning(
                        "[%s] AI Card skipped: missing sender_staff_id for DM",
                        self.name,
                    )
                    return None
                open_space_id = f"dtv1.card//IM_ROBOT.{sender_staff_id}"
                deliver_request = dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=open_space_id,
                    im_robot_open_deliver_model=(
                        dingtalk_card_models.DeliverCardRequestImRobotOpenDeliverModel(
                            space_type="IM_ROBOT",
                        )
                    ),
                )

            deliver_headers = dingtalk_card_models.DeliverCardHeaders(
                x_acs_dingtalk_access_token=token,
            )

            delivery_response = await self._card_sdk.deliver_card_with_options_async(
                deliver_request, deliver_headers, runtime
            )

            # Stream initial content; finalize=False leaves it open for edits.
            await self._stream_card_content(
                out_track_id, token, content, finalize=finalize,
            )
            reply_context_saved = self._card_reply_store.remember_delivery(
                chat_id, out_track_id, content, delivery_response
            )

            logger.info(
                "[%s] AI Card %s: %s",
                self.name,
                "created+finalized" if finalize else "created (streaming)",
                out_track_id,
            )
            return SendResult(success=True, message_id=out_track_id,
                              raw_response={"reply_context_saved": reply_context_saved})

        except Exception as e:
            logger.warning(
                "[%s] AI Card create failed: %s\n%s",
                self.name, e, traceback.format_exc(),
            )
            return None

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit one AI Card using the out_track_id returned by its initial send."""
        if not message_id:
            return SendResult(success=False, error="message_id required")
        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            if not self._card_reply_store.invalidate_content(chat_id, message_id):
                return SendResult(success=False, error="Cannot safely update saved card original")
            await self._stream_card_content(
                message_id, token, content, finalize=finalize,
            )
            reply_context_saved = self._card_reply_store.update_content(chat_id, message_id, content)
            if finalize:
                # Final edit ends tracking and fires Done.
                self._streaming_cards.get(chat_id, {}).pop(message_id, None)
                if not self._streaming_cards.get(chat_id):
                    self._streaming_cards.pop(chat_id, None)
                logger.debug(
                    "[%s] AI Card finalized (edit): %s",
                    self.name, message_id,
                )
                self._fire_done_reaction(chat_id)
                mark_h1_turn_delivered(self, chat_id=chat_id, message_id=message_id)
            else:
                # Track non-final edits so the next send can close the card.
                self._streaming_cards.setdefault(chat_id, {})[message_id] = content
            return SendResult(success=True, message_id=message_id,
                              raw_response={"reply_context_saved": reply_context_saved})
        except Exception as e:
            logger.warning("[%s] Card edit failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _stream_card_content(
        self,
        out_track_id: str,
        token: str,
        content: str,
        finalize: bool = False,
    ) -> None:
        """Stream content to an existing AI Card."""
        stream_request = dingtalk_card_models.StreamingUpdateRequest(
            out_track_id=out_track_id,
            guid=str(uuid.uuid4()),
            key="content",
            content=content,
            is_full=True,
            is_finalize=finalize,
            is_error=False,
        )

        stream_headers = dingtalk_card_models.StreamingUpdateHeaders(
            x_acs_dingtalk_access_token=token,
        )

        runtime = tea_util_models.RuntimeOptions()
        await self._card_sdk.streaming_update_with_options_async(
            stream_request, stream_headers, runtime
        )

    async def _get_access_token(self) -> Optional[str]:
        """Get access token using SDK's cached token."""
        if not self._stream_client:
            return None
        try:
            # SDK's get_access_token is sync and uses requests
            token = await asyncio.to_thread(self._stream_client.get_access_token)
            return token
        except Exception as e:
            logger.error("[%s] Failed to get access token: %s", self.name, e)
            return None

    async def _send_emotion(
        self,
        open_msg_id: str,
        open_conversation_id: str,
        emoji_name: str,
        *,
        recall: bool = False,
    ) -> None:
        """Add or recall an emoji reaction on a message."""
        if not self._robot_sdk or not open_msg_id or not open_conversation_id:
            return
        action = "recall" if recall else "reply"
        try:
            token = await self._get_access_token()
            if not token:
                return

            emotion_kwargs = {
                "robot_code": self._robot_code,
                "open_msg_id": open_msg_id,
                "open_conversation_id": open_conversation_id,
                "emotion_type": 2,
                "emotion_name": emoji_name,
            }
            runtime = tea_util_models.RuntimeOptions()

            if recall:
                emotion_kwargs["text_emotion"] = (
                    dingtalk_robot_models.RobotRecallEmotionRequestTextEmotion(
                        emotion_id="2659900",
                        emotion_name=emoji_name,
                        text=emoji_name,
                        background_id="im_bg_1",
                    )
                )
                request = dingtalk_robot_models.RobotRecallEmotionRequest(
                    **emotion_kwargs,
                )
                sdk_headers = dingtalk_robot_models.RobotRecallEmotionHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                await self._robot_sdk.robot_recall_emotion_with_options_async(
                    request, sdk_headers, runtime
                )
            else:
                emotion_kwargs["text_emotion"] = (
                    dingtalk_robot_models.RobotReplyEmotionRequestTextEmotion(
                        emotion_id="2659900",
                        emotion_name=emoji_name,
                        text=emoji_name,
                        background_id="im_bg_1",
                    )
                )
                request = dingtalk_robot_models.RobotReplyEmotionRequest(
                    **emotion_kwargs,
                )
                sdk_headers = dingtalk_robot_models.RobotReplyEmotionHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                await self._robot_sdk.robot_reply_emotion_with_options_async(
                    request, sdk_headers, runtime
                )
            logger.info(
                "[%s] _send_emotion: %s %s has_message_id=%s",
                self.name, action, emoji_name, bool(open_msg_id),
            )
        except Exception:
            logger.debug(
                "[%s] _send_emotion %s failed", self.name, action, exc_info=True
            )

    async def _resolve_media_codes(self, message: "ChatbotMessage") -> None:
        """Resolve download codes in message to actual URLs."""
        token = await self._get_access_token()
        if not token:
            return

        msg_robot_code = getattr(message, "robot_code", None)
        robot_code = self._client_id or msg_robot_code
        logger.info(
            "[%s] media dl robotCode expt-C: using_client_id=%s msg_rc_matches=%s",
            self.name,
            bool(self._client_id),
            msg_robot_code == self._client_id,
        )
        codes_to_resolve = []

        img_content = getattr(message, "image_content", None)
        if img_content and getattr(img_content, "download_code", None):
            codes_to_resolve.append((img_content, "download_code"))

        rich_text = getattr(message, "rich_text_content", None) or getattr(
            message, "rich_text", None
        )
        if rich_text:
            rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
            for item in rich_list:
                if isinstance(item, dict):
                    for key in ("downloadCode", "pictureDownloadCode", "download_code"):
                        if item.get(key):
                            codes_to_resolve.append((item, key))

        replied_file = _get_replied_file_content(message)
        if replied_file:
            for key in ("downloadCode", "download_code"):
                if replied_file.get(key):
                    codes_to_resolve.append((replied_file, key))

        if not codes_to_resolve:
            return

        tasks = []
        for obj, key in codes_to_resolve:
            code = getattr(obj, key, None) if hasattr(obj, key) else obj.get(key)
            if code:
                tasks.append(self._fetch_download_url(code, robot_code, token, obj, key))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_download_url(
        self, code: str, robot_code: str, token: str, obj, key: str
    ) -> None:
        """Fetch download URL for a single code using the robot SDK."""
        if not self._robot_sdk:
            logger.warning(
                "[%s] Robot SDK not initialized, cannot resolve media code",
                self.name,
            )
            return
        last_err = None
        for attempt in range(3):
            try:
                request = dingtalk_robot_models.RobotMessageFileDownloadRequest(
                    download_code=code,
                    robot_code=robot_code,
                )
                headers = dingtalk_robot_models.RobotMessageFileDownloadHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                runtime = tea_util_models.RuntimeOptions()
                response = await self._robot_sdk.robot_message_file_download_with_options_async(
                    request, headers, runtime
                )
                body = response.body if response else None
                if body:
                    url = getattr(body, "download_url", None)
                    if url:
                        filename = ""
                        if isinstance(obj, dict):
                            filename = str(obj.get("fileName") or "")
                        if not filename:
                            path = url.split("?", 1)[0].rstrip("/")
                            basename = path.rsplit("/", 1)[-1] if "/" in path else ""
                            filename = basename if ("." in basename) else "dingtalk_image.png"
                        cached = await self._cache_downloaded_file_url(url, filename)
                        if cached:
                            url = cached
                        if hasattr(obj, key):
                            setattr(obj, key, url)
                        elif isinstance(obj, dict):
                            obj[key] = url
                    if attempt > 0:
                        logger.info(
                            "[%s] media download OK on retry #%d for key=%s",
                            self.name,
                            attempt,
                            key,
                        )
                    return
                logger.warning(
                    "[%s] media download empty response for key=%s attempt=%d",
                    self.name,
                    key,
                    attempt,
                )
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "[%s] media download attempt=%d failed key=%s error_type=%s provider_code_present=%s",
                    self.name,
                    attempt,
                    key,
                    type(exc).__name__,
                    bool(getattr(exc, "code", None)),
                )
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
        logger.error(
            "[%s] Error resolving media key=%s after 3 attempts error_type=%s provider_code_present=%s",
            self.name,
            key,
            type(last_err).__name__ if last_err else "Unknown",
            bool(getattr(last_err, "code", None)) if last_err else False,
        )

    async def _cache_downloaded_file_url(self, url: str, filename: str) -> Optional[str]:
        """Download a DingTalk file URL and cache it for the agent."""
        if not HTTPX_AVAILABLE:
            return None
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "HermesAgent/1.0",
                        "Accept": "*/*",
                    },
                )
                response.raise_for_status()
            cached = cache_media_bytes(
                response.content,
                filename=filename,
                mime_type=mime_type,
                default_kind="document",
            )
            if cached:
                logger.info(
                    "[%s] Cached DingTalk file type=%s",
                    self.name,
                    cached.media_type,
                )
                return cached.path
        except Exception as exc:
            logger.warning(
                "[%s] Failed to cache DingTalk file ext=%s error_type=%s",
                self.name,
                os.path.splitext(str(filename))[1].lower()[:16],
                type(exc).__name__,
            )
        return None

# ──────────────────────────────────────────────────────────────────────────
# Plugin migration glue (#41112 / #3823)
#
# Added when the DingTalk adapter moved from gateway/platforms/dingtalk.py into
# this bundled plugin. Mirrors the Discord (#24356) / Slack migrations: a
# register(ctx) entry point plus hook implementations that replace the
# per-platform core touchpoints (the Platform.DINGTALK elif in gateway/run.py,
# the dingtalk_cfg YAML→env block + _PLATFORM_CONNECTED_CHECKERS entry in
# gateway/config.py, the _setup_dingtalk wizard + _PLATFORMS["dingtalk"] static
# dict in hermes_cli/gateway.py, and the _send_dingtalk dispatch in
# tools/send_message_tool.py).
# ──────────────────────────────────────────────────────────────────────────

def _build_adapter(config):
    """Factory wrapper that constructs DingTalkAdapter from a PlatformConfig."""
    return DingTalkAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="dingtalk",
        label="DingTalk",
        adapter_factory=_build_adapter,
        check_fn=dingtalk_deps_present,
        ensure_deps_fn=ensure_dingtalk_deps,
        is_connected=_is_connected,
        validate_config=_is_connected,
        required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
        install_hint="pip install 'dingtalk-stream>=0.20' httpx",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="DINGTALK_ALLOWED_USERS",
        allow_all_env="DINGTALK_ALLOW_ALL_USERS",
        cron_deliver_env_var="DINGTALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        emoji="🐳",
        allow_update_command=True,
    )
    # Admin-gated 1:1 private-message tool (config-driven allowlist; see private_send.py).
    # A tool-registration failure must never break the platform adapter itself.
    try:
        from .private_send import register_private_send_tool

        register_private_send_tool(ctx)
    except Exception:  # noqa: BLE001 — optional tool; degrade without killing the platform
        logging.getLogger(__name__).warning(
            "DingTalk private-send tool registration failed", exc_info=True
        )
