"""DingTalk Stream Mode incoming callback handler."""

import asyncio
import json
from typing import Any, Optional


def make_incoming_handler(
    *,
    dingtalk_stream: Any,
    dingtalk_stream_available: bool,
    chatbot_message_cls: Any,
    ack_message_cls: Any,
    logger: Any,
    log_forward_diag: Any,
):
    """Build a ChatbotHandler subclass bound to the optional SDK globals."""

    base = dingtalk_stream.ChatbotHandler if dingtalk_stream_available else object

    class IncomingHandler(base):
        """dingtalk-stream ChatbotHandler that forwards messages to the adapter."""

        def __init__(self, adapter: Any, loop: Optional[asyncio.AbstractEventLoop] = None):
            if dingtalk_stream_available:
                super().__init__()
            self._adapter = adapter
            self._loop = loop

        def pre_start(self) -> None:
            """No-op pre-start hook required by dingtalk-stream SDK."""
            return

        async def raw_process(self, callback_message):
            """Compatibility hook for dingtalk-stream versions that call raw_process()."""
            data = getattr(callback_message, "data", callback_message)
            if hasattr(callback_message, "data"):
                message = callback_message
            else:
                message = type("_CompatCallbackMessage", (), {"data": data})()
            code, response = await self.process(message)
            ack_message = ack_message_cls()
            ack_message.code = code
            ack_message.headers.message_id = getattr(
                getattr(callback_message, "headers", None), "message_id", None
            )
            ack_message.headers.content_type = "application/json"
            ack_message.data = {"response": response}
            return ack_message

        async def process(self, message: "CallbackMessage"):
            """Called by dingtalk-stream (>=0.20) when a message arrives."""
            try:
                data = message.data
                if isinstance(data, str):
                    data = json.loads(data)
                log_forward_diag("raw_callback", data)

                chatbot_msg = chatbot_message_cls.from_dict(data)

                if not getattr(chatbot_msg, "session_webhook", None):
                    webhook = (
                        data.get("sessionWebhook")
                        or data.get("session_webhook")
                        or ""
                    ) if isinstance(data, dict) else ""
                    if webhook:
                        chatbot_msg.session_webhook = webhook
                if not getattr(chatbot_msg, "message_type", None) and isinstance(data, dict):
                    chatbot_msg.message_type = data.get("msgtype") or data.get("messageType") or ""

                if not getattr(chatbot_msg, "is_in_at_list", False):
                    raw_flag = (
                        data.get("isInAtList") if isinstance(data, dict) else False
                    )
                    if raw_flag:
                        chatbot_msg.is_in_at_list = True

                chatbot_msg._hermes_raw_data = data

                msg_id = getattr(chatbot_msg, "message_id", None) or ""
                conversation_id = getattr(chatbot_msg, "conversation_id", None) or ""

                if msg_id and conversation_id:
                    self._adapter._spawn_bg(
                        self._adapter._send_emotion(
                            msg_id, conversation_id, "🤔Thinking", recall=False,
                        )
                    )

                asyncio.create_task(self._safe_on_message(chatbot_msg))
            except Exception:
                logger.exception(
                    "[%s] Error preparing incoming message", self._adapter.name
                )
                return ack_message_cls.STATUS_SYSTEM_EXCEPTION, "error"

            return ack_message_cls.STATUS_OK, "OK"

        async def _safe_on_message(self, chatbot_msg: "ChatbotMessage") -> None:
            """Wrapper that catches exceptions from _on_message."""
            try:
                await self._adapter._on_message(chatbot_msg)
            except Exception:
                logger.exception(
                    "[%s] Error processing incoming message", self._adapter.name
                )

    return IncomingHandler
