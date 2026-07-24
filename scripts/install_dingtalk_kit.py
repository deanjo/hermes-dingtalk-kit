#!/usr/bin/env python3
"""Install Hermes DingTalk Kit into a Hermes root."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPAT_PATCHER = ROOT / "scripts/compat_patcher.py"
POST_INSTALL_VERIFIER = ROOT / "scripts/post_install_verifier.py"
SOURCE_PLUGIN_DIR = ROOT / "overlays/hermes/plugins/platforms/dingtalk"
PLUGIN_REL = Path("plugins/platforms/dingtalk")
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
SOURCE_PRODUCT_PLUGIN_DIR = ROOT / "overlays/hermes/plugins/product_confirmation"
PRODUCT_PLUGIN_REL = Path("plugins/product_confirmation")
PRODUCT_PLUGIN_FILES = (
    "__init__.py",
    "plugin.yaml",
    "store.py",
    "tools.py",
)
SOURCE_H1_PLUGIN_DIR = ROOT / "overlays/hermes/plugins/h1_task_write"
H1_PLUGIN_REL = Path("plugins/h1_task_write")
H1_PLUGIN_FILES = (
    "__init__.py",
    "plugin.yaml",
    "tools.py",
)
CORE_REL_PATHS = (
    Path("gateway/run.py"),
    Path("gateway/session.py"),
    Path("gateway/session_context.py"),
)


@dataclass
class OperationResult:
    name: str
    path: str
    status: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        item = {"name": self.name, "path": self.path, "status": self.status}
        if self.message:
            item["message"] = self.message
        return item


@dataclass
class FileSnapshot:
    path: Path
    existed: bool
    data: bytes = b""
    mode: int = 0o644


@dataclass
class TreeSnapshot:
    path: Path
    existed: bool
    backup: Path | None = None


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_compat_patcher() -> Any:
    return _load_module(COMPAT_PATCHER, "hermes_dingtalk_compat_patcher_for_installer")


def _load_post_install_verifier() -> Any:
    return _load_module(POST_INSTALL_VERIFIER, "hermes_dingtalk_post_install_verifier_for_installer")


def _ok(name: str, path: str, message: str = "") -> OperationResult:
    return OperationResult(name=name, path=path, status="ok", message=message)


def _fail(name: str, path: str, status: str, message: str) -> OperationResult:
    return OperationResult(name=name, path=path, status=status, message=message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_manifest(
    source_dir: Path,
    filenames: tuple[str, ...] = PLUGIN_FILES,
) -> dict[str, dict[str, str | int | bool]]:
    manifest: dict[str, dict[str, str | int | bool]] = {}
    for filename in filenames:
        path = source_dir / filename
        manifest[filename] = {
            "exists": path.is_file(),
            "sha256": _sha256(path) if path.is_file() else "",
            "bytes": path.stat().st_size if path.is_file() else 0,
        }
    return manifest


def _validate_source_plugin(
    source_dir: Path,
    filenames: tuple[str, ...] = PLUGIN_FILES,
    operation_name: str = "plugin.source",
) -> OperationResult:
    if not source_dir.is_dir():
        return _fail(
            operation_name,
            str(source_dir),
            "missing-source",
            "plugin source directory not found",
        )
    missing = [filename for filename in filenames if not (source_dir / filename).is_file()]
    if missing:
        return _fail(
            operation_name,
            str(source_dir),
            "missing-source",
            "missing plugin files: " + ", ".join(missing),
        )
    return _ok(operation_name, str(source_dir), f"{len(filenames)} files")


def _take_file_snapshots(root: Path) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    for rel_path in CORE_REL_PATHS:
        path = root / rel_path
        if path.exists():
            snapshots.append(
                FileSnapshot(
                    path=path,
                    existed=True,
                    data=path.read_bytes(),
                    mode=stat.S_IMODE(path.stat().st_mode),
                )
            )
        else:
            snapshots.append(FileSnapshot(path=path, existed=False))
    return snapshots


def _restore_file_snapshots(snapshots: list[FileSnapshot]) -> None:
    for snapshot in snapshots:
        if snapshot.existed:
            snapshot.path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.path.write_bytes(snapshot.data)
            snapshot.path.chmod(snapshot.mode)
        elif snapshot.path.exists():
            snapshot.path.unlink()


def _take_tree_snapshot(
    path: Path,
    backup_root: Path,
    backup_name: str = "previous_dingtalk_plugin",
) -> TreeSnapshot:
    if not path.exists():
        return TreeSnapshot(path=path, existed=False)
    backup = backup_root / backup_name
    shutil.copytree(path, backup)
    return TreeSnapshot(path=path, existed=True, backup=backup)


def _restore_tree_snapshot(snapshot: TreeSnapshot) -> None:
    if snapshot.path.exists():
        shutil.rmtree(snapshot.path)
    if snapshot.existed:
        if snapshot.backup is None:
            raise RuntimeError("missing plugin backup for restore")
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot.backup, snapshot.path)


def _plugin_tree_matches(
    source_dir: Path,
    target_dir: Path,
    filenames: tuple[str, ...] = PLUGIN_FILES,
) -> bool:
    if not target_dir.is_dir():
        return False
    for filename in filenames:
        source_file = source_dir / filename
        target_file = target_dir / filename
        if not target_file.is_file():
            return False
        if source_file.read_bytes() != target_file.read_bytes():
            return False
    # Import-time bytecode is runtime cache, not installed source.  Ignoring
    # only ``__pycache__`` keeps repeated installs idempotent after the verifier
    # imports the plugin while still replacing any undeclared source file.
    extra_paths = [
        path for path in target_dir.iterdir()
        if path.name not in filenames and path.name != "__pycache__"
    ]
    return not extra_paths


def _rename_tree(source: Path, destination: Path) -> None:
    """Rename one directory within a filesystem (test seam for fault injection)."""
    source.rename(destination)


def _install_plugin_tree(
    source_dir: Path,
    target_dir: Path,
    temp_root: Path,
    filenames: tuple[str, ...] = PLUGIN_FILES,
    operation_name: str = "plugin.copy",
    staged_name: str = "staged_dingtalk_plugin",
) -> OperationResult:
    if _plugin_tree_matches(source_dir, target_dir, filenames):
        return _ok(operation_name, str(target_dir), "plugin tree already matches source")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the destination so every rename stays on the destination's
    # filesystem. ``temp_root`` remains the transaction-wide snapshot root; it
    # is intentionally not used for the live switch.
    del temp_root
    staged = Path(
        tempfile.mkdtemp(
            prefix=f".{target_dir.name}.{staged_name}-",
            dir=target_dir.parent,
        )
    )
    previous = target_dir.parent / (
        f".{target_dir.name}.previous-{uuid.uuid4().hex}"
    )
    try:
        staged.chmod(stat.S_IMODE(source_dir.stat().st_mode))
        # Copy only the declared manifest. Development artifacts such as
        # ``__pycache__`` must never become part of an installed plugin tree.
        for filename in filenames:
            shutil.copy2(source_dir / filename, staged / filename)

        if target_dir.exists():
            _rename_tree(target_dir, previous)
        try:
            _rename_tree(staged, target_dir)
        except Exception:
            # The new-tree rename failed after the old tree moved aside. Put
            # the old tree back before propagating the error; outer rollback
            # remains a second safety net for the multi-tree transaction.
            if previous.exists() and not target_dir.exists():
                _rename_tree(previous, target_dir)
            raise

        if previous.exists():
            shutil.rmtree(previous)
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        # Safe cleanup after a successful switch or successful restoration.
        # If restoration itself failed and target is absent, preserve
        # ``previous`` so the transaction-level rollback can recover it.
        if previous.exists() and target_dir.exists():
            shutil.rmtree(previous, ignore_errors=True)
    return _ok(operation_name, str(target_dir), f"copied {len(filenames)} files")


def _rollback(
    operations: list[OperationResult],
    core_snapshots: list[FileSnapshot],
    plugin_snapshots: list[TreeSnapshot],
) -> None:
    try:
        _restore_file_snapshots(core_snapshots)
        for plugin_snapshot in plugin_snapshots:
            _restore_tree_snapshot(plugin_snapshot)
    except Exception as exc:  # noqa: BLE001 - installer rollback boundary
        operations.append(
            _fail("rollback", ".", "rollback-failed", f"{type(exc).__name__}: {exc}")
        )
    else:
        operations.append(_ok("rollback", ".", "restored pre-install files"))


def _operation_failures(operations: list[OperationResult]) -> list[OperationResult]:
    return [
        operation
        for operation in operations
        if operation.status
        in {"error", "failed", "missing-source", "missing-file", "rollback-failed"}
    ]


def _core_manifest(root: Path) -> dict[str, dict[str, str | int | bool]]:
    manifest: dict[str, dict[str, str | int | bool]] = {}
    for rel_path in CORE_REL_PATHS:
        path = root / rel_path
        exists = path.is_file()
        manifest[str(rel_path)] = {
            "exists": exists,
            "sha256": _sha256(path) if exists else "",
            "bytes": path.stat().st_size if exists else 0,
        }
    return manifest


def _core_changed_paths(
    before: dict[str, dict[str, str | int | bool]],
    after: dict[str, dict[str, str | int | bool]],
) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def build_report(target: Path, *, plugins_only: bool = False) -> dict[str, Any]:
    """Install Kit-owned plugin trees, optionally applying legacy core compat.

    plugins_only is the T4 offline-verification mode: it installs the same
    DingTalk and Product Confirmation source trees but intentionally skips the
    pre-existing legacy gateway compat patch set. The report keeps plugin-owned
    files and legacy core changes in separate fields.
    """
    operations: list[OperationResult] = []
    compat_report: dict[str, Any] | None = None
    verifier_report: dict[str, Any] | None = None
    root = target
    mode = "plugins-only" if plugins_only else "legacy-compat"
    plugin_manifest = {
        "dingtalk": _source_manifest(SOURCE_PLUGIN_DIR),
        "product_confirmation": _source_manifest(
            SOURCE_PRODUCT_PLUGIN_DIR, PRODUCT_PLUGIN_FILES
        ),
        "h1_task_write": _source_manifest(
            SOURCE_H1_PLUGIN_DIR, H1_PLUGIN_FILES
        ),
    }
    owned_plugin_paths = [str(PLUGIN_REL), str(PRODUCT_PLUGIN_REL), str(H1_PLUGIN_REL)]

    def finish(
        *,
        core_before: dict[str, dict[str, str | int | bool]] | None = None,
    ) -> dict[str, Any]:
        before = core_before or {}
        after = _core_manifest(root) if before else {}
        failures = _operation_failures(operations)
        return {
            "ok": not failures,
            "target": str(root),
            "installation_mode": mode,
            "operation_count": len(operations),
            "failure_count": len(failures),
            "operations": [operation.to_dict() for operation in operations],
            "owned_plugin_paths": owned_plugin_paths,
            "plugin_manifest": plugin_manifest,
            "legacy_compat": {
                "requested": not plugins_only,
                "applied": compat_report is not None,
                "rolled_back": any(
                    operation.name == "rollback" for operation in operations
                ),
                "result": compat_report,
            },
            "core_manifest": {
                "before": before,
                "after": after,
                "changed_paths": _core_changed_paths(before, after),
            },
            "compat": compat_report,
            "verifier": verifier_report,
        }

    try:
        compat = _load_compat_patcher()
        verifier = _load_post_install_verifier()
        root = compat.resolve_target(target)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        operations.append(
            _fail("target.resolve", str(target), "error", f"{type(exc).__name__}: {exc}")
        )
        return finish()

    operations.append(_ok("target.resolve", str(root), "Hermes root located"))

    source_checks = [
        _validate_source_plugin(
            SOURCE_PLUGIN_DIR,
            PLUGIN_FILES,
            "dingtalk.source",
        ),
        _validate_source_plugin(
            SOURCE_PRODUCT_PLUGIN_DIR,
            PRODUCT_PLUGIN_FILES,
            "product_confirmation.source",
        ),
        _validate_source_plugin(
            SOURCE_H1_PLUGIN_DIR,
            H1_PLUGIN_FILES,
            "h1_task_write.source",
        ),
    ]
    operations.extend(source_checks)
    if any(check.status != "ok" for check in source_checks):
        return finish()

    core_before = _core_manifest(root)
    core_snapshots = _take_file_snapshots(root)
    plugin_snapshots: list[TreeSnapshot] = []
    should_rollback = False

    with tempfile.TemporaryDirectory(prefix="hermes-dingtalk-install-") as temp_name:
        temp_root = Path(temp_name)
        try:
            plugin_snapshots = [
                _take_tree_snapshot(
                    root / PLUGIN_REL,
                    temp_root,
                    "previous_dingtalk_plugin",
                ),
                _take_tree_snapshot(
                    root / PRODUCT_PLUGIN_REL,
                    temp_root,
                    "previous_product_confirmation_plugin",
                ),
                _take_tree_snapshot(
                    root / H1_PLUGIN_REL,
                    temp_root,
                    "previous_h1_task_write_plugin",
                ),
            ]
            operations.append(
                _install_plugin_tree(
                    SOURCE_PLUGIN_DIR,
                    root / PLUGIN_REL,
                    temp_root,
                    PLUGIN_FILES,
                    "dingtalk.copy",
                    "staged_dingtalk_plugin",
                )
            )
            operations.append(
                _install_plugin_tree(
                    SOURCE_PRODUCT_PLUGIN_DIR,
                    root / PRODUCT_PLUGIN_REL,
                    temp_root,
                    PRODUCT_PLUGIN_FILES,
                    "product_confirmation.copy",
                    "staged_product_confirmation_plugin",
                )
            )
            operations.append(
                _install_plugin_tree(
                    SOURCE_H1_PLUGIN_DIR,
                    root / H1_PLUGIN_REL,
                    temp_root,
                    H1_PLUGIN_FILES,
                    "h1_task_write.copy",
                    "staged_h1_task_write_plugin",
                )
            )

            if plugins_only:
                operations.append(
                    _ok(
                        "compat.skip",
                        ".",
                        "legacy gateway compat intentionally excluded from Product verification",
                    )
                )
            else:
                compat_report = compat.build_report(root, "apply")
                compat_message = (
                    f"changed_count={compat_report['changed_count']} "
                    f"failure_count={compat_report['failure_count']}"
                )
                if compat_report["ok"]:
                    operations.append(_ok("compat.apply", ".", compat_message))
                else:
                    operations.append(_fail("compat.apply", ".", "failed", compat_message))
                    should_rollback = True

            if not should_rollback:
                if plugins_only:
                    verifier_report = verifier.build_report(root, require_compat=False)
                else:
                    verifier_report = verifier.build_report(root)
                verifier_message = (
                    f"check_count={verifier_report['check_count']} "
                    f"failure_count={verifier_report['failure_count']}"
                )
                if verifier_report["ok"]:
                    operations.append(_ok("post_install.verify", ".", verifier_message))
                else:
                    operations.append(_fail("post_install.verify", ".", "failed", verifier_message))
                    should_rollback = True
        except Exception as exc:  # noqa: BLE001 - installer boundary
            operations.append(
                _fail("install", str(root), "error", f"{type(exc).__name__}: {exc}")
            )
            should_rollback = True

        if should_rollback:
            _rollback(operations, core_snapshots, plugin_snapshots)

    return finish(core_before=core_before)



def _text_summary(report: dict[str, Any]) -> str:
    status = "ok" if report["ok"] else "failed"
    lines = [
        f"dingtalk kit installer {status}",
        f"target={report['target']}",
        f"operation_count={report['operation_count']} failure_count={report['failure_count']}",
    ]
    for item in report["operations"]:
        suffix = f" ({item['message']})" if item.get("message") else ""
        lines.append(f"- {item['status']}: {item['name']} [{item['path']}]{suffix}")
    compat = report.get("compat")
    if isinstance(compat, dict):
        lines.append(
            "compat="
            f"ok={compat['ok']} changed_count={compat['changed_count']} "
            f"failure_count={compat['failure_count']}"
        )
    verifier = report.get("verifier")
    if isinstance(verifier, dict):
        lines.append(
            "verifier="
            f"ok={verifier['ok']} check_count={verifier['check_count']} "
            f"failure_count={verifier['failure_count']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Hermes DingTalk Kit into a Hermes root.")
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Hermes root containing gateway/, or a parent containing hermes/gateway/.",
    )
    parser.add_argument(
        "--plugins-only",
        action="store_true",
        help=(
            "Install and verify Kit-owned plugin trees without applying the "
            "pre-existing legacy gateway compatibility patch set."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    report = build_report(args.target, plugins_only=args.plugins_only)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_text_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
