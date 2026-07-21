#!/usr/bin/env python3
"""Verify a Hermes root after installing Hermes DingTalk Kit."""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COMPAT_PATCHER = ROOT / "scripts/compat_patcher.py"
MAX_SOURCE_LINES = 1500
RUNTIME_PROBE_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
)
PLUGIN_FILES = (
    "__init__.py",
    "adapter.py",
    "incoming.py",
    "markdown.py",
    "media.py",
    "mentions.py",
    "plugin_setup.py",
    "plugin.yaml",
    "private_send.py",
    "reply_context.py",
    "task_binding.py",
)
PLUGIN_REL = Path("plugins/platforms/dingtalk")
PRODUCT_PLUGIN_FILES = (
    "__init__.py",
    "plugin.yaml",
    "store.py",
    "tools.py",
)
PRODUCT_PLUGIN_REL = Path("plugins/product_confirmation")
FAILURE_STATUSES = {"error", "failed", "missing-file"}


@dataclass
class CheckResult:
    name: str
    path: str
    status: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        item = {"name": self.name, "path": self.path, "status": self.status}
        if self.message:
            item["message"] = self.message
        return item


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_compat_patcher() -> Any:
    return _load_module(COMPAT_PATCHER, "hermes_dingtalk_compat_patcher_for_verifier")


def _ok(name: str, path: str, message: str = "") -> CheckResult:
    return CheckResult(name=name, path=path, status="ok", message=message)


def _fail(name: str, path: str, status: str, message: str) -> CheckResult:
    return CheckResult(name=name, path=path, status=status, message=message)


def _skip(name: str, path: str, message: str) -> CheckResult:
    return CheckResult(name=name, path=path, status="skipped", message=message)


def _run_check(name: str, path: str, func: Callable[[], str]) -> CheckResult:
    try:
        message = func()
    except AssertionError as exc:
        return _fail(name, path, "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - verifier boundary
        return _fail(name, path, "error", f"{type(exc).__name__}: {exc}")
    return _ok(name, path, message)


def _read_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _has_dingtalk_register_call(path: Path) -> bool:
    tree = _read_tree(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "register_platform":
            continue
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                if keyword.value.value == "dingtalk":
                    return True
    return False


def _assert_plugin_entry(root: Path) -> str:
    init_py = root / PLUGIN_REL / "__init__.py"
    init_text = init_py.read_text(encoding="utf-8")
    if "from .adapter import register" not in init_text:
        raise AssertionError("__init__.py does not export adapter.register")
    adapter = root / PLUGIN_REL / "adapter.py"
    text = adapter.read_text(encoding="utf-8")
    required = [
        "class DingTalkAdapter",
        "def register(ctx)",
        "make_incoming_handler",
        "build_reply_kwargs",
    ]
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise AssertionError("missing markers: " + ", ".join(missing))
    if not _has_dingtalk_register_call(adapter):
        raise AssertionError("ctx.register_platform(name=\"dingtalk\") not found")
    return "DingTalkAdapter/register entry is present"


def _assert_plugin_manifest(root: Path) -> str:
    manifest = root / PLUGIN_REL / "plugin.yaml"
    text = manifest.read_text(encoding="utf-8")
    required = [
        "name: dingtalk-platform",
        "label: DingTalk",
        "kind: platform",
        "version:",
        "requires_env:",
        "DINGTALK_CLIENT_ID",
        "DINGTALK_CLIENT_SECRET",
    ]
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise AssertionError("missing manifest markers: " + ", ".join(missing))
    return "plugin.yaml declares dingtalk-platform kind=platform"


def _runtime_discovery_probe(root: Path) -> CheckResult:
    try:
        return _runtime_discovery_probe_inner(root)
    except AssertionError as exc:
        return _fail("plugin.runtime_discovery", ".", "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - verifier boundary
        return _fail("plugin.runtime_discovery", ".", "error", f"{type(exc).__name__}: {exc}")


def _runtime_discovery_probe_inner(root: Path) -> CheckResult:
    required_runtime_files = [
        root / "hermes_cli/plugins.py",
        root / "gateway/platform_registry.py",
        root / "gateway/config.py",
    ]
    if not all(path.is_file() for path in required_runtime_files):
        return _skip(
            "plugin.runtime_discovery",
            ".",
            "Hermes runtime modules not present in target root",
        )

    code = """
from hermes_cli.plugins import get_plugin_manager
from gateway.platform_registry import platform_registry

manager = get_plugin_manager()
manager.discover_and_load(force=True)
entry = platform_registry.get("dingtalk")
if entry is None:
    raise SystemExit("dingtalk entry missing")
if entry.name != "dingtalk":
    raise SystemExit(f"unexpected platform name: {entry.name!r}")
if entry.plugin_name != "dingtalk-platform":
    raise SystemExit(f"unexpected plugin name: {entry.plugin_name!r}")
if not platform_registry.is_registered("dingtalk"):
    raise SystemExit("dingtalk not registered")
print(f"runtime_dingtalk_entry={entry.name} plugin={entry.plugin_name}")
""".strip()
    with tempfile.TemporaryDirectory(prefix="hermes-dingtalk-runtime-home-") as temp_home:
        env = _runtime_probe_env(root, temp_home)
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=str(root),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    if completed.returncode != 0:
        raise AssertionError(f"runtime discovery failed with exit code {completed.returncode}")
    summary = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    if "runtime_dingtalk_entry=dingtalk plugin=dingtalk-platform" not in summary:
        raise AssertionError("runtime discovery did not report DingTalk entry")
    return _ok("plugin.runtime_discovery", ".", summary)


def _runtime_probe_env(root: Path, temp_home: str) -> dict[str, str]:
    env = {
        name: value
        for name in RUNTIME_PROBE_ENV_ALLOWLIST
        if (value := os.environ.get(name))
    }
    env["HERMES_HOME"] = temp_home
    env["HERMES_BUNDLED_PLUGINS"] = str(root / "plugins")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(root)
    return env


def _assert_raw_process_ack(root: Path) -> str:
    incoming = _load_module(
        root / PLUGIN_REL / "incoming.py",
        f"post_install_incoming_{id(root)}",
    )

    class FakeAck:
        STATUS_OK = 200
        STATUS_SYSTEM_EXCEPTION = 500

        def __init__(self) -> None:
            self.code: int | None = None
            self.headers = types.SimpleNamespace(message_id=None, content_type="")
            self.data: dict[str, str] = {}

    handler_cls = incoming.make_incoming_handler(
        dingtalk_stream=types.SimpleNamespace(ChatbotHandler=object),
        dingtalk_stream_available=False,
        chatbot_message_cls=object,
        ack_message_cls=FakeAck,
        logger=types.SimpleNamespace(exception=lambda *args, **kwargs: None),
        log_forward_diag=lambda *args, **kwargs: None,
    )
    handler = handler_cls(adapter=object())

    async def fake_process(message: Any) -> tuple[int, str]:
        assert getattr(message, "data", None) == {"hello": "world"}
        return FakeAck.STATUS_OK, "OK"

    handler.process = fake_process
    callback = types.SimpleNamespace(
        data={"hello": "world"},
        headers=types.SimpleNamespace(message_id="mid-1"),
    )
    ack = asyncio.run(handler.raw_process(callback))
    if not isinstance(ack, FakeAck):
        raise AssertionError("raw_process did not return AckMessage instance")
    if ack.code != FakeAck.STATUS_OK:
        raise AssertionError(f"unexpected ack code: {ack.code!r}")
    if ack.headers.message_id != "mid-1":
        raise AssertionError("callback message_id was not copied")
    if ack.headers.content_type != "application/json":
        raise AssertionError("ack content_type is not application/json")
    if ack.data != {"response": "OK"}:
        raise AssertionError(f"unexpected ack data: {ack.data!r}")
    return "raw_process returns AckMessage with OK response"


def _assert_reply_context(root: Path) -> str:
    reply_context = _load_module(
        root / PLUGIN_REL / "reply_context.py",
        f"post_install_reply_context_{id(root)}",
    )

    class Text:
        extensions = {
            "repliedMsg": {
                "msgId": "reply-42",
                "msgType": "text",
            }
        }

    class Message:
        text = Text()

    kwargs = reply_context.build_reply_kwargs(Message())
    if kwargs.get("reply_to_message_id") != "reply-42":
        raise AssertionError(f"unexpected reply_to_message_id: {kwargs!r}")
    if kwargs.get("reply_to_text") != reply_context._REPLY_ORIGINAL_UNAVAILABLE:
        raise AssertionError("missing reply original sentinel fallback")
    if kwargs.get("reply_to_is_own_message") is not False:
        raise AssertionError("reply_to_is_own_message must be False")

    class FileText:
        extensions = {
            "repliedMsg": {
                "msgId": "file-1",
                "msgType": "file",
                "content": {"fileName": "x.txt"},
            }
        }

    class FileMessage:
        text = FileText()

    if reply_context.build_reply_kwargs(FileMessage()) != {}:
        raise AssertionError("file replies must stay on the file-content path")
    return "repliedMsg maps to reply_to_message_id/reply_to_text"


def _assert_reply_context_fail_closed(root: Path) -> str:
    """Ensure unresolved text replies stop before model dispatch."""
    def is_reply_lookup(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "reply_kwargs"
            and node.func.attr == "get"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "reply_to_text"
        )

    def is_unavailable_sentinel(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "_REPLY_ORIGINAL_UNAVAILABLE"

    adapter = root / PLUGIN_REL / "adapter.py"
    tree = _read_tree(adapter)
    clarification = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_REPLY_CONTEXT_CLARIFICATION"
            for target in node.targets
        ):
            try:
                clarification = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                clarification = None
            break
    if not isinstance(clarification, str) or not clarification.strip():
        raise AssertionError("reply-context clarification text is missing")

    method = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DingTalkAdapter":
            method = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "_on_message"
                ),
                None,
            )
            break
    if method is None:
        raise AssertionError("DingTalkAdapter._on_message not found")

    guard_index = None
    dispatch_index = None
    for index, statement in enumerate(method.body):
        if isinstance(statement, ast.If):
            test = statement.test
            has_exact_guard = (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and (
                    (
                        is_reply_lookup(test.left)
                        and is_unavailable_sentinel(test.comparators[0])
                    )
                    or (
                        is_unavailable_sentinel(test.left)
                        and is_reply_lookup(test.comparators[0])
                    )
                )
            )
            if has_exact_guard:
                guard_index = index
                all_sends = [
                    node.value
                    for node in ast.walk(statement)
                    if isinstance(node, ast.Await)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == "self"
                    and node.value.func.attr == "send"
                ]
                direct_sends = []
                for body_index, body_statement in enumerate(statement.body):
                    await_node = None
                    if isinstance(body_statement, ast.Assign):
                        await_node = body_statement.value
                    elif isinstance(body_statement, ast.Expr):
                        await_node = body_statement.value
                    if (
                        isinstance(await_node, ast.Await)
                        and isinstance(await_node.value, ast.Call)
                        and isinstance(await_node.value.func, ast.Attribute)
                        and isinstance(await_node.value.func.value, ast.Name)
                        and await_node.value.func.value.id == "self"
                        and await_node.value.func.attr == "send"
                    ):
                        direct_sends.append((body_index, await_node.value))
                if len(all_sends) != 1 or len(direct_sends) != 1:
                    raise AssertionError("unresolved-reply guard must await exactly one send")
                send_index, send_call = direct_sends[0]
                if not (
                    len(send_call.args) == 2
                    and isinstance(send_call.args[0], ast.Name)
                    and send_call.args[0].id == "chat_id"
                    and isinstance(send_call.args[1], ast.Name)
                    and send_call.args[1].id == "_REPLY_CONTEXT_CLARIFICATION"
                ):
                    raise AssertionError("unresolved-reply guard sends the wrong chat or content")
                if not any(
                    keyword.arg == "reply_to"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "msg_id"
                    for keyword in send_call.keywords
                ):
                    raise AssertionError("unresolved-reply guard does not reply to the incoming message")
                all_returns = [
                    node for node in ast.walk(statement) if isinstance(node, ast.Return)
                ]
                direct_returns = [
                    body_index
                    for body_index, body_statement in enumerate(statement.body)
                    if isinstance(body_statement, ast.Return)
                ]
                if len(all_returns) != 1 or len(direct_returns) != 1:
                    raise AssertionError("unresolved-reply guard does not return before dispatch")
                if send_index >= direct_returns[0]:
                    raise AssertionError("unresolved-reply guard returns before clarification send")
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "handle_message"
            for node in ast.walk(statement)
        ):
            dispatch_index = index

    if guard_index is None:
        raise AssertionError("unresolved-reply guard not found")
    if dispatch_index is None or guard_index >= dispatch_index:
        raise AssertionError("unresolved-reply guard must run before handle_message")
    return "unresolved text replies clarify once and stop before model dispatch"


def _load_function_from_ast(path: Path, function_name: str) -> Any:
    tree = _read_tree(path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace: dict[str, Any] = {}
            exec(compile(module, str(path), "exec"), namespace)
            return namespace[function_name]
    raise AssertionError(f"{function_name} not found")


def _assert_session_key_slash(root: Path) -> str:
    checker = _load_function_from_ast(root / "gateway/session.py", "_is_session_key_unsafe")
    allowed = [
        "agent:main:dingtalk:dm:cidEXAMPLEbase64/WithSlashAAAA=",
        "agent:main:dingtalk:group:cidEXAMPLEbase64/WithSlash==:10000000000000001",
        "agent:main:dingtalk:dm:$:LWCP_v1:$EXAMPLE/EncryptedIdAA==",
    ]
    blocked = [
        "../../etc/passwd",
        "agent:..:x",
        "/etc/passwd",
        "~/x",
        "C:/windows/system32",
        "C:\\windows",
        "a\\b",
    ]
    bad_allowed = [value for value in allowed if checker(value)]
    bad_blocked = [value for value in blocked if not checker(value)]
    if bad_allowed:
        raise AssertionError("DingTalk base64 keys rejected: " + ", ".join(bad_allowed))
    if bad_blocked:
        raise AssertionError("path-shaped keys allowed: " + ", ".join(bad_blocked))
    return "DingTalk base64 slash keys allowed and path-shaped keys blocked"


def _build_source_signature_probe(root: Path) -> CheckResult:
    """Guard the adapter ↔ Core ``build_source`` keyword seam.

    Compares every keyword the plugin adapter passes to ``self.build_source``
    against the signature of ``BasePlatformAdapter.build_source`` in the
    TARGET root, so a Core-side signature change (or an adapter-side kwarg
    Core never accepted, e.g. the Kanban binding fields) fails verification
    instead of raising TypeError on the production inbound path.
    """
    name = "plugin.build_source_signature"
    rel = str(PLUGIN_REL / "adapter.py")
    base_py = root / "gateway/platforms/base.py"
    if not base_py.is_file():
        # Offline baseline roots lack the core platform tree; allow pointing
        # at a real Core checkout so CI can enforce the seam instead of
        # skipping it.
        core_root = os.environ.get("HERMES_DINGTALK_CORE_ROOT", "").strip()
        candidate = Path(core_root) / "gateway/platforms/base.py" if core_root else None
        if candidate is not None and candidate.is_file():
            base_py = candidate
        else:
            return _skip(name, "gateway/platforms/base.py", "core platform base not present in target root")
    try:
        accepted: set[str] | None = None
        for node in _read_tree(base_py).body:
            if not (isinstance(node, ast.ClassDef) and node.name == "BasePlatformAdapter"):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "build_source":
                    accepted = {arg.arg for arg in item.args.args + item.args.kwonlyargs}
        if accepted is None:
            raise AssertionError("BasePlatformAdapter.build_source not found")
        accepted.discard("self")
        calls = [
            node
            for node in ast.walk(_read_tree(root / PLUGIN_REL / "adapter.py"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_source"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ]
        if not calls:
            raise AssertionError("adapter never calls self.build_source")
        for call in calls:
            if any(keyword.arg is None for keyword in call.keywords):
                raise AssertionError("self.build_source must use explicit keywords (no **kwargs)")
            unknown = sorted(
                keyword.arg for keyword in call.keywords if keyword.arg not in accepted
            )
            if unknown:
                raise AssertionError("kwargs unsupported by core build_source: " + ", ".join(unknown))
    except AssertionError as exc:
        return _fail(name, rel, "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - verifier boundary
        return _fail(name, rel, "error", f"{type(exc).__name__}: {exc}")
    return _ok(name, rel, "adapter kwargs accepted by core build_source")


def _assert_session_context_bridge(root: Path) -> str:
    module = _load_module(
        root / "gateway/session_context.py",
        f"post_install_session_context_{id(root)}",
    )
    tokens = module.set_session_vars(
        chat_type="group",
        user_id_alt="staff-42",
        session_id="sid-42",
    )
    try:
        expected = {
            "HERMES_SESSION_CHAT_TYPE": "group",
            "HERMES_SESSION_USER_ID_ALT": "staff-42",
            "HERMES_SESSION_ID": "sid-42",
        }
        for name, value in expected.items():
            actual = module.get_session_env(name)
            if actual != value:
                raise AssertionError(f"{name}={actual!r}, expected {value!r}")
    finally:
        module.clear_session_vars(tokens)

    for name in (
        "HERMES_SESSION_CHAT_TYPE",
        "HERMES_SESSION_USER_ID_ALT",
        "HERMES_SESSION_ID",
    ):
        actual = module.get_session_env(name)
        if actual != "":
            raise AssertionError(f"{name} was not cleared: {actual!r}")
    return "session context bridge sets and clears DingTalk fields"


def _check_plugin_files(
    root: Path,
    plugin_rel: Path = PLUGIN_REL,
    filenames: tuple[str, ...] = PLUGIN_FILES,
    name_prefix: str = "plugin",
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for filename in filenames:
        rel_path = plugin_rel / filename
        path = root / rel_path
        name = f"{name_prefix}.file.{filename}"
        if not path.exists():
            results.append(_fail(name, str(rel_path), "missing-file", "required plugin file not found"))
            continue
        results.append(_ok(name, str(rel_path), "file exists"))
        if path.suffix != ".py":
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        line_name = f"{name_prefix}.line_count.{filename}"
        if line_count > MAX_SOURCE_LINES:
            results.append(
                _fail(
                    line_name,
                    str(rel_path),
                    "failed",
                    f"{line_count} lines exceeds limit {MAX_SOURCE_LINES}",
                )
            )
        else:
            results.append(_ok(line_name, str(rel_path), f"{line_count} lines"))
    return results


def _assert_product_plugin(root: Path) -> str:
    """Load Product Confirmation as a package and inspect its public surface."""
    plugin_dir = root / PRODUCT_PLUGIN_REL
    module_name = f"post_install_product_confirmation_{id(root)}"
    for loaded_name in list(sys.modules):
        if loaded_name == module_name or loaded_name.startswith(module_name + "."):
            sys.modules.pop(loaded_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load product_confirmation package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)

        class FakeContext:
            def __init__(self) -> None:
                self.hooks: list[tuple[str, Any]] = []
                self.tools: list[str] = []

            def register_hook(self, name: str, callback: Any) -> None:
                self.hooks.append((name, callback))

            def register_tool(self, *, name: str, **kwargs: Any) -> None:
                self.tools.append(name)

        context = FakeContext()
        module.register(context)
        expected_tools = {
            "product_confirm_draft",
            "product_confirm_request",
            "product_confirm_decide",
            "product_confirm_advance",
            "product_confirm_status",
        }
        if set(context.tools) != expected_tools:
            raise AssertionError(f"unexpected Product tools: {sorted(context.tools)!r}")
        if [name for name, _ in context.hooks] != ["pre_gateway_dispatch"]:
            raise AssertionError(f"unexpected Product hooks: {context.hooks!r}")
    finally:
        for loaded_name in list(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(module_name + "."):
                sys.modules.pop(loaded_name, None)

    tools_text = (plugin_dir / "tools.py").read_text(encoding="utf-8")
    forbidden = [
        "gateway.run import _gateway_runner_ref",
        "HERMES_SESSION_USER_ID_ALT",
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
    ]
    present = [marker for marker in forbidden if marker in tools_text]
    if present:
        raise AssertionError("private gateway bridge markers present: " + ", ".join(present))
    required = [
        'getattr(source, "user_id_alt"',
        "capture_dispatch_context",
        "asyncio.run_coroutine_threadsafe",
    ]
    missing = [marker for marker in required if marker not in tools_text]
    if missing:
        raise AssertionError("missing public hook wiring: " + ", ".join(missing))
    return "five Product tools plus pre_gateway_dispatch hook registered"


def _assert_product_manifest(root: Path) -> str:
    text = (root / PRODUCT_PLUGIN_REL / "plugin.yaml").read_text(encoding="utf-8")
    required = [
        "name: product_confirmation",
        "kind: standalone",
        "product_confirm_draft",
        "product_confirm_request",
        "product_confirm_decide",
        "product_confirm_advance",
        "product_confirm_status",
    ]
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise AssertionError("missing Product manifest markers: " + ", ".join(missing))
    return "Product manifest declares standalone plugin and five tools"


def _product_hook_contract_probe(root: Path) -> CheckResult:
    hook_registry = root / "hermes_cli/plugins.py"
    gateway_run = root / "gateway/run.py"
    if not hook_registry.is_file() or not gateway_run.is_file():
        return _skip(
            "product.public_hook_contract",
            ".",
            "full Hermes public hook runtime not present in target root",
        )
    try:
        registry_text = hook_registry.read_text(encoding="utf-8")
        run_text = gateway_run.read_text(encoding="utf-8")
        if '"pre_gateway_dispatch"' not in registry_text:
            raise AssertionError("pre_gateway_dispatch absent from VALID_HOOKS")
        required = [
            '"pre_gateway_dispatch",',
            "event=event,",
            "gateway=self,",
            "session_store=self.session_store,",
        ]
        missing = [marker for marker in required if marker not in run_text]
        if missing:
            raise AssertionError("gateway hook kwargs missing: " + ", ".join(missing))
    except AssertionError as exc:
        return _fail("product.public_hook_contract", ".", "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - verifier boundary
        return _fail(
            "product.public_hook_contract",
            ".",
            "error",
            f"{type(exc).__name__}: {exc}",
        )
    return _ok(
        "product.public_hook_contract",
        ".",
        "event, gateway and session_store public kwargs found",
    )


def _compat_results(root: Path) -> list[CheckResult]:
    compat = _load_compat_patcher()
    report = compat.build_report(root, "verify")
    results = [
        _ok(
            "compat.verify",
            ".",
            f"failure_count={report['failure_count']} changed_count={report['changed_count']}",
        )
        if report["ok"]
        else _fail(
            "compat.verify",
            ".",
            "failed",
            f"failure_count={report['failure_count']} changed_count={report['changed_count']}",
        )
    ]
    for item in report["results"]:
        status = item["status"]
        name = "compat." + item["name"]
        path = item["path"]
        message = item.get("message", "")
        if status == "ok":
            results.append(_ok(name, path, message))
        else:
            results.append(_fail(name, path, status, message))
    return results


def _has_failure(results: list[CheckResult]) -> bool:
    return any(result.status in FAILURE_STATUSES for result in results)


def build_report(target: Path, *, require_compat: bool = True) -> dict[str, Any]:
    results: list[CheckResult] = []
    try:
        compat = _load_compat_patcher()
        root = compat.resolve_target(target)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        root = target
        results.append(
            _fail(
                "target.resolve",
                str(target),
                "error",
                f"{type(exc).__name__}: {exc}",
            )
        )
    else:
        results.append(_ok("target.resolve", str(root), "Hermes root located"))
        compat_results = _compat_results(root) if require_compat else []
        results.extend(compat_results)
        if not _has_failure(compat_results):
            plugin_results = _check_plugin_files(root)
            product_results = _check_plugin_files(
                root,
                PRODUCT_PLUGIN_REL,
                PRODUCT_PLUGIN_FILES,
                "product",
            )
            results.extend(plugin_results + product_results)
            if not _has_failure(plugin_results + product_results):
                results.extend(
                    [
                        _run_check("plugin.manifest", str(PLUGIN_REL / "plugin.yaml"), lambda: _assert_plugin_manifest(root)),
                        _run_check("plugin.entry", str(PLUGIN_REL / "adapter.py"), lambda: _assert_plugin_entry(root)),
                        _build_source_signature_probe(root),
                        _runtime_discovery_probe(root),
                        _run_check("plugin.raw_process_ack", str(PLUGIN_REL / "incoming.py"), lambda: _assert_raw_process_ack(root)),
                        _run_check("plugin.reply_context_kwargs", str(PLUGIN_REL / "reply_context.py"), lambda: _assert_reply_context(root)),
                        _run_check("plugin.reply_context_fail_closed", str(PLUGIN_REL / "adapter.py"), lambda: _assert_reply_context_fail_closed(root)),
                        _run_check("product.manifest", str(PRODUCT_PLUGIN_REL / "plugin.yaml"), lambda: _assert_product_manifest(root)),
                        _run_check("product.entry", str(PRODUCT_PLUGIN_REL / "__init__.py"), lambda: _assert_product_plugin(root)),
                        _product_hook_contract_probe(root),
                    ]
                )
                if require_compat:
                    results.extend(
                        [
                            _run_check("gateway.session_key_slash", "gateway/session.py", lambda: _assert_session_key_slash(root)),
                            _run_check("gateway.session_context_bridge", "gateway/session_context.py", lambda: _assert_session_context_bridge(root)),
                        ]
                    )

    failures = [result for result in results if result.status in FAILURE_STATUSES]
    return {
        "ok": not failures,
        "target": str(root),
        "check_count": len(results),
        "failure_count": len(failures),
        "require_compat": require_compat,
        "checks": [result.to_dict() for result in results],
    }


def _text_summary(report: dict[str, Any]) -> str:
    status = "ok" if report["ok"] else "failed"
    lines = [
        f"post-install verifier {status}",
        f"target={report['target']}",
        f"check_count={report['check_count']} failure_count={report['failure_count']}",
    ]
    for item in report["checks"]:
        suffix = f" ({item['message']})" if item.get("message") else ""
        lines.append(f"- {item['status']}: {item['name']} [{item['path']}]{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Hermes DingTalk Kit after installation.")
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Hermes root containing gateway/ and plugins/platforms/dingtalk/.",
    )
    parser.add_argument(
        "--plugins-only",
        action="store_true",
        help="Verify Kit-owned plugin trees without requiring legacy core compat markers.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    report = build_report(args.target, require_compat=not args.plugins_only)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_text_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
