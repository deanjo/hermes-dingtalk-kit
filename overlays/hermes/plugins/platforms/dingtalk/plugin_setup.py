"""DingTalk plugin registration support functions."""

import os


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process DingTalk delivery via a static robot webhook URL."""
    extra = getattr(pconfig, "extra", {}) or {}
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}
    try:
        webhook_url = extra.get("webhook_url") or os.getenv("DINGTALK_WEBHOOK_URL", "")
        if not webhook_url:
            return {"error": "DingTalk not configured. Set DINGTALK_WEBHOOK_URL env var or webhook_url in dingtalk platform extra config."}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                webhook_url,
                json={"msgtype": "text", "text": {"content": message}},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode", 0) != 0:
                return {"error": f"DingTalk API error: {data.get('errmsg', 'unknown')}"}
        return {"success": True, "platform": "dingtalk", "chat_id": chat_id}
    except Exception as e:
        try:
            from tools.send_message_tool import _error as _redact_error
            return _redact_error(f"DingTalk send failed: {e}")
        except Exception:
            return {"error": f"DingTalk send failed: {e}"}


def interactive_setup() -> None:
    """Configure DingTalk by QR scan or manual credential entry."""
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.setup import prompt_choice
    from hermes_cli.cli_output import (
        prompt,
        prompt_yes_no,
        print_header,
        print_success,
        print_warning,
    )

    print_header("DingTalk")
    existing = get_env_value("DINGTALK_CLIENT_ID")
    if existing:
        print_success(f"DingTalk is already configured (Client ID: {existing}).")
        if not prompt_yes_no("Reconfigure DingTalk?", False):
            return

    method = prompt_choice(
        "Choose setup method",
        [
            "QR Code Scan (Recommended, auto-obtain Client ID and Client Secret)",
            "Manual Input (Client ID and Client Secret)",
        ],
        default=0,
    )

    if method == 0:
        try:
            from hermes_cli.dingtalk_auth import dingtalk_qr_auth
        except ImportError as exc:
            print_warning(f"QR auth module failed to load ({exc}), falling back to manual input.")
            _manual_credential_entry(prompt, save_env_value, print_success)
            return
        result = dingtalk_qr_auth()
        if result is None:
            print_warning("QR auth incomplete, falling back to manual input.")
            _manual_credential_entry(prompt, save_env_value, print_success)
            return
        client_id, client_secret = result
        save_env_value("DINGTALK_CLIENT_ID", client_id)
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
        print_success("DingTalk configured via QR scan!")
    else:
        _manual_credential_entry(prompt, save_env_value, print_success)


def _manual_credential_entry(prompt, save_env_value, print_success) -> None:
    client_id = prompt("DingTalk Client ID (app key)")
    if not client_id:
        return
    save_env_value("DINGTALK_CLIENT_ID", client_id)
    client_secret = prompt("DingTalk Client Secret", password=True)
    if client_secret:
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
    print_success("DingTalk credentials saved")


def _apply_yaml_config(yaml_cfg: dict, dingtalk_cfg: dict) -> dict | None:
    """Translate config.yaml dingtalk: keys into DINGTALK_* env vars."""
    import json as _json
    if "require_mention" in dingtalk_cfg and not os.getenv("DINGTALK_REQUIRE_MENTION"):
        os.environ["DINGTALK_REQUIRE_MENTION"] = str(dingtalk_cfg["require_mention"]).lower()
    if "mention_patterns" in dingtalk_cfg and not os.getenv("DINGTALK_MENTION_PATTERNS"):
        os.environ["DINGTALK_MENTION_PATTERNS"] = _json.dumps(dingtalk_cfg["mention_patterns"])
    frc = dingtalk_cfg.get("free_response_chats")
    if frc is not None and not os.getenv("DINGTALK_FREE_RESPONSE_CHATS"):
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        os.environ["DINGTALK_FREE_RESPONSE_CHATS"] = str(frc)
    ac = dingtalk_cfg.get("allowed_chats")
    if ac is not None and not os.getenv("DINGTALK_ALLOWED_CHATS"):
        if isinstance(ac, list):
            ac = ",".join(str(v) for v in ac)
        os.environ["DINGTALK_ALLOWED_CHATS"] = str(ac)
    allowed = dingtalk_cfg.get("allowed_users")
    if allowed is not None and not os.getenv("DINGTALK_ALLOWED_USERS"):
        if isinstance(allowed, list):
            allowed = ",".join(str(v) for v in allowed)
        os.environ["DINGTALK_ALLOWED_USERS"] = str(allowed)
    return None


def _is_connected(config) -> bool:
    """DingTalk is connected when client_id + client_secret are present."""
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID"))
        and (extra.get("client_secret") or os.getenv("DINGTALK_CLIENT_SECRET"))
    )
