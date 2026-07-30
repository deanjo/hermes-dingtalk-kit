"""守护两张"打包清单"与磁盘实际文件一致。

为什么需要这两条断言：H1 治理第 6 项实测发现两处"本地测试全绿、产出物却是坏的"
缺陷，本地 pytest 一条都抓不住，与该项目历史上 44/44 假绿同型——

1. `scripts/compat_patcher.py` 在模块顶层加载同级模块 `compat_native_shapes`，
   但 `docker/Dockerfile` 的 COPY 没把它搬进镜像 → 镜像内一加载就 FileNotFoundError
   → installer 兜成安装失败并回滚 → `docker build` 非零退出。
2. installer / verifier 两份 `PLUGIN_FILES` 都漏了 `delivery_gate.py`，而
   `_install_plugin_tree` 只复制声明在清单里的文件，`adapter.py` 又在模块顶层
   `from .delivery_gate import blocked_send_result` → 装出来的插件一 import 就炸。

共同根因：**源码树里存在的文件，与打包清单里声明的文件，没有任何自动对账**。
下面两条测试就是这个对账。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker/Dockerfile"
SCRIPTS_DIR = ROOT / "scripts"
INSTALLER = SCRIPTS_DIR / "install_dingtalk_kit.py"
VERIFIER = SCRIPTS_DIR / "post_install_verifier.py"

# 本仓库全部「按清单打包」的插件，一行一个：
#   (overlays 下的源码目录相对路径, installer 里的清单常量名, verifier 里的清单常量名)
#
# 这张表是唯一的插件枚举，`tests/test_install_dingtalk_kit.py` 的 Dockerfile COPY
# 断言也从这里读——新增第四个插件时只需要加一行，两处断言自动罩住。
#
# 为什么要数据驱动：H1 治理第 6 项的独立验收发现，上一轮只把 dingtalk 一个目录
# 写成常量硬编码进对账，于是在 `h1_task_write/` 下放一个未登记的 `.py`，全量
# pytest 仍然 230 passed 全绿——检查网只罩住了三个插件里的一个。
PLUGIN_MANIFESTS = (
    (
        "overlays/hermes/plugins/platforms/dingtalk",
        "PLUGIN_FILES",
        "PLUGIN_FILES",
    ),
    (
        "overlays/hermes/plugins/product_confirmation",
        "PRODUCT_PLUGIN_FILES",
        "PRODUCT_PLUGIN_FILES",
    ),
    (
        "overlays/hermes/plugins/h1_task_write",
        "H1_PLUGIN_FILES",
        "H1_PLUGIN_FILES",
    ),
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dockerfile_copied_scripts() -> set[str]:
    """Dockerfile 里被 COPY 进镜像的 scripts/ 文件名集合。

    判据：取所有 `COPY <src>... <dst>` 行中以 `scripts/` 开头的源路径，
    末段（目标目录）不算源。只认字面路径，不展开通配符——本仓库的
    Dockerfile 逐个列出脚本，一旦有人改成通配符，这里会读出空集合而
    让测试红，那是正确的提醒（需要重新设计本断言）。
    """
    copied: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("COPY "):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        for src in tokens[:-1]:  # 最后一个 token 是镜像内目标路径
            if src.startswith("scripts/"):
                copied.add(Path(src).name)
    return copied


def _sibling_module_deps(path: Path) -> set[str]:
    """某个脚本通过 `_load_sibling_module("X")` 依赖的同级模块名集合。

    判据：用 ast 找出所有 `_load_sibling_module(<字面字符串>)` 调用点，
    **不区分是否在模块顶层**——顶层调用会在 import 时立刻炸（本次缺陷一），
    函数体内的调用只是把爆炸推迟到运行时，同样是坏镜像，一并守住。
    非字面参数（变量拼出来的模块名）无法静态判定，这里忽略；本仓库当前没有
    这种写法。
    """
    deps: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "_load_sibling_module" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            deps.add(arg.value)
    return deps


class DockerfileScriptClosureTest(unittest.TestCase):
    """缺陷一的防再犯断言。"""

    def test_copied_scripts_carry_their_sibling_module_deps(self):
        """被 COPY 进镜像的脚本，其同级模块依赖也必须被 COPY。

        判据：从 Dockerfile 声明的脚本出发做传递闭包——每个脚本
        `_load_sibling_module("X")` 依赖的 `X.py`，必须同样出现在 COPY 声明里。
        镜像里 `/opt/hermes-dingtalk-kit/scripts/` 只有 COPY 进去的文件，
        没进去的同级依赖在镜像内就是不存在的文件。

        失败时的正确处置是把缺的脚本加进 `docker/Dockerfile` 的 COPY 行，
        而不是放宽这条断言。
        """
        copied = _dockerfile_copied_scripts()
        self.assertIn(
            "compat_patcher.py",
            copied,
            "Dockerfile 未 COPY compat_patcher.py，本断言的前提已变，请复核",
        )

        missing = []
        seen: set[str] = set()
        pending = sorted(copied)
        while pending:
            filename = pending.pop()
            if filename in seen:
                continue
            seen.add(filename)
            source = SCRIPTS_DIR / filename
            if not source.exists():
                missing.append(f"{filename}: Dockerfile 声明了但 scripts/ 下不存在")
                continue
            for dep in sorted(_sibling_module_deps(source)):
                dep_file = f"{dep}.py"
                if dep_file not in copied:
                    missing.append(
                        f"{dep_file}: 被 scripts/{filename} 顶层/运行期加载，"
                        f"但未出现在 docker/Dockerfile 的 COPY 声明中"
                    )
                pending.append(dep_file)

        self.assertEqual([], missing)


def _packable_source_files(source_dir: Path) -> set[str]:
    """源码目录里「应当被打包」的顶层文件名集合。

    判据（H1 治理第 6 项 P3 放宽）：**不再只认 `*.py`**。清单里本来就声明了
    `plugin.yaml` 这类非 py 资源，`_install_plugin_tree` 也是照清单逐个
    `shutil.copy2` 平铺复制，与扩展名无关——所以判据必须覆盖"清单声明的全部
    文件类型"，否则新增一个 `.yaml` / `.json` 资源却忘了登记时照样抓不住。
    具体口径：
    - 只扫**顶层**：平铺复制本来就不支持子目录，`__pycache__` 这类目录被
      `is_file()` 直接挡在外面。
    - 排除**隐藏文件**（`.` 开头，如 `.DS_Store`）：不是源码，不该进包。
    - **`__init__.py` 不豁免**：缺了它安装后的目录就不是一个包，
      `from .delivery_gate import ...` 这类相对导入直接失效。
    - 方向是「实际文件 ⊆ 声明清单」。新增源文件却不登记 → 红。
    """
    return {
        entry.name
        for entry in source_dir.iterdir()
        if entry.is_file() and not entry.name.startswith(".")
    }


class PluginManifestTest(unittest.TestCase):
    """缺陷二的防再犯断言，对 PLUGIN_MANIFESTS 里的每个插件都成立。"""

    def setUp(self):
        self.installer = _load(INSTALLER, "hermes_dingtalk_installer_manifest_gate")
        self.verifier = _load(VERIFIER, "hermes_dingtalk_verifier_manifest_gate")

    def test_installer_manifest_covers_every_source_file(self):
        """每个插件源码目录里的顶层文件都要登记进对应的 installer 清单。

        口径见 `_packable_source_files` 的 docstring。失败时的正确处置是把文件
        补进 `scripts/install_dingtalk_kit.py` 的清单（以及 verifier 的同名
        清单），而不是放宽这条断言。
        """
        for rel_dir, installer_attr, _ in PLUGIN_MANIFESTS:
            with self.subTest(plugin=rel_dir):
                source_dir = ROOT / rel_dir
                self.assertTrue(
                    source_dir.is_dir(),
                    f"{rel_dir} 不存在，本断言的前提已变，请复核 PLUGIN_MANIFESTS",
                )
                actual = _packable_source_files(source_dir)
                self.assertIn(
                    "__init__.py",
                    actual,
                    f"{rel_dir} 源码目录形状异常，请复核本断言前提",
                )
                declared = set(getattr(self.installer, installer_attr))
                undeclared = sorted(actual - declared)
                self.assertEqual(
                    [],
                    undeclared,
                    f"以下源文件未登记进 install_dingtalk_kit.py 的 "
                    f"{installer_attr}，安装后的插件目录会缺这些文件",
                )

    def test_verifier_manifest_matches_installer_manifest(self):
        """verifier 的每份清单必须与 installer 的同名清单完全一致。

        判据：这两份清单是同一份事实的两份拷贝（一份决定装什么，一份决定验什么）。
        本次缺陷二正是两份同时漏了 `delivery_gate.py`；只补一份则 verifier 会
        对着装好的目录漏验或误报。逐字段相等是唯一安全的关系。
        """
        for rel_dir, installer_attr, verifier_attr in PLUGIN_MANIFESTS:
            with self.subTest(plugin=rel_dir):
                self.assertEqual(
                    sorted(getattr(self.installer, installer_attr)),
                    sorted(getattr(self.verifier, verifier_attr)),
                    f"installer.{installer_attr} 与 verifier.{verifier_attr} 不一致",
                )


if __name__ == "__main__":
    unittest.main()
