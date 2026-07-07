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
    "reply_context.py",
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


def _source_manifest(source_dir: Path) -> dict[str, dict[str, str | int | bool]]:
    manifest: dict[str, dict[str, str | int | bool]] = {}
    for filename in PLUGIN_FILES:
        path = source_dir / filename
        manifest[filename] = {
            "exists": path.is_file(),
            "sha256": _sha256(path) if path.is_file() else "",
            "bytes": path.stat().st_size if path.is_file() else 0,
        }
    return manifest


def _validate_source_plugin(source_dir: Path) -> OperationResult:
    if not source_dir.is_dir():
        return _fail(
            "plugin.source",
            str(source_dir),
            "missing-source",
            "DingTalk plugin source directory not found",
        )
    missing = [filename for filename in PLUGIN_FILES if not (source_dir / filename).is_file()]
    if missing:
        return _fail(
            "plugin.source",
            str(source_dir),
            "missing-source",
            "missing plugin files: " + ", ".join(missing),
        )
    return _ok("plugin.source", str(source_dir), f"{len(PLUGIN_FILES)} files")


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


def _take_tree_snapshot(path: Path, backup_root: Path) -> TreeSnapshot:
    if not path.exists():
        return TreeSnapshot(path=path, existed=False)
    backup = backup_root / "previous_dingtalk_plugin"
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


def _plugin_tree_matches(source_dir: Path, target_dir: Path) -> bool:
    if not target_dir.is_dir():
        return False
    for filename in PLUGIN_FILES:
        source_file = source_dir / filename
        target_file = target_dir / filename
        if not target_file.is_file():
            return False
        if source_file.read_bytes() != target_file.read_bytes():
            return False
    extra_paths = [path for path in target_dir.iterdir() if path.name not in PLUGIN_FILES]
    return not extra_paths


def _install_plugin_tree(source_dir: Path, target_dir: Path, temp_root: Path) -> OperationResult:
    if _plugin_tree_matches(source_dir, target_dir):
        return _ok("plugin.copy", str(target_dir), "plugin tree already matches source")

    staged = temp_root / "staged_dingtalk_plugin"
    shutil.copytree(source_dir, staged)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(target_dir))
    return _ok("plugin.copy", str(target_dir), f"copied {len(PLUGIN_FILES)} files")


def _rollback(
    operations: list[OperationResult],
    core_snapshots: list[FileSnapshot],
    plugin_snapshot: TreeSnapshot | None,
) -> None:
    try:
        _restore_file_snapshots(core_snapshots)
        if plugin_snapshot is not None:
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


def build_report(target: Path) -> dict[str, Any]:
    operations: list[OperationResult] = []
    compat_report: dict[str, Any] | None = None
    verifier_report: dict[str, Any] | None = None
    root = target

    try:
        compat = _load_compat_patcher()
        verifier = _load_post_install_verifier()
        root = compat.resolve_target(target)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        operations.append(
            _fail("target.resolve", str(target), "error", f"{type(exc).__name__}: {exc}")
        )
        failures = _operation_failures(operations)
        return {
            "ok": False,
            "target": str(root),
            "operation_count": len(operations),
            "failure_count": len(failures),
            "operations": [operation.to_dict() for operation in operations],
            "plugin_manifest": {"source": {}},
            "compat": compat_report,
            "verifier": verifier_report,
        }

    operations.append(_ok("target.resolve", str(root), "Hermes root located"))

    source_check = _validate_source_plugin(SOURCE_PLUGIN_DIR)
    operations.append(source_check)
    if source_check.status != "ok":
        failures = _operation_failures(operations)
        return {
            "ok": False,
            "target": str(root),
            "operation_count": len(operations),
            "failure_count": len(failures),
            "operations": [operation.to_dict() for operation in operations],
            "plugin_manifest": {"source": _source_manifest(SOURCE_PLUGIN_DIR)},
            "compat": compat_report,
            "verifier": verifier_report,
        }

    core_snapshots = _take_file_snapshots(root)
    plugin_snapshot: TreeSnapshot | None = None
    should_rollback = False

    with tempfile.TemporaryDirectory(prefix="hermes-dingtalk-install-") as temp_name:
        temp_root = Path(temp_name)
        try:
            plugin_snapshot = _take_tree_snapshot(root / PLUGIN_REL, temp_root)
            operations.append(_install_plugin_tree(SOURCE_PLUGIN_DIR, root / PLUGIN_REL, temp_root))

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
            _rollback(operations, core_snapshots, plugin_snapshot)

    failures = _operation_failures(operations)
    return {
        "ok": not failures,
        "target": str(root),
        "operation_count": len(operations),
        "failure_count": len(failures),
        "operations": [operation.to_dict() for operation in operations],
        "plugin_manifest": {"source": _source_manifest(SOURCE_PLUGIN_DIR)},
        "compat": compat_report,
        "verifier": verifier_report,
    }


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
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    report = build_report(args.target)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_text_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
