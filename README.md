# Hermes DingTalk Kit

Hermes DingTalk Kit 是 Hermes 官方 DingTalk adapter（适配器）的生产补丁包，用来减少接入钉钉时反复踩的坑。

当前状态：private staging（私有整理仓库）。代码基于 `hermes-agent:stable-daocloud` 验证；已拆出 4 个 upstream（上游）PR，但发布成功不依赖官方合并。

## 包含内容

- `raw_process` / `AckMessage` 兼容：修复 `dingtalk-stream 0.24.3` 调用 `handler.raw_process(msg)` 时官方 adapter 不响应的问题。
- 引用上下文注入：把钉钉 `repliedMsg` 转成 Hermes `reply_to_message_id` / `reply_to_text`，避免群里“这个问题”错绑到最近话题。
- 重启不失忆：放行 DingTalk base64 conversation id 中的内部 `/`，同时保留 `session_id` 的严格路径校验。
- 会话环境修复：向 `set_session_vars(...)` 补传 `session_id`，让插件稳定读取 `HERMES_SESSION_ID`。
- 群内 @ 元信息：从钉钉结构化 `atUsers` 还原“本消息还 @ 了谁”，并在发送侧用 `at_user_ids` 生成结构化 @。
- 发送结果校验：HTTP 200 但响应体 `errcode != 0` 时按失败处理，不再把“机器人已移出群”等拒绝误报成成功。
- Product Confirmation（产品确认）插件：5 个窄工具维护 `PRODUCT_DRAFT -> WAITING_PRODUCT_CONFIRMATION -> PRODUCT_APPROVED/PRODUCT_NEEDS_REVISION -> TECH_DESIGN`；状态和发送 claim（占用权）存 SQLite，同一 `task_id + proposal_version` 并发时只有 claim 获得者可以发送。明确未送达会标为 `FAILED` 后允许重试，结果不确定则保留 `CLAIMED` 并阻止自动重发；身份来自公开 `pre_gateway_dispatch` hook 的 `event.source.user_id_alt`。

## 不包含内容

- 不包含任何 `.env`、API key、token、cookie、Authorization header。
- 不包含公司特定社区运营策略。
- 不包含 Discourse 服务器插件；Discourse 的钉钉登录插件是另一个项目。

## 快速构建

```bash
docker build \
  --build-arg HERMES_BASE=hermes-agent:stable-daocloud \
  -t hermes-agent:dingtalk-kit \
  -f docker/Dockerfile .
```

## 目录

```text
docker/        Docker build entrypoint
overlays/      可直接 COPY 到 Hermes 镜像的当前补丁文件
patches/       与 `hermes-agent:stable-daocloud` 的 unified diff
tests/         最小静态回归测试
scripts/       发布前检查脚本
docs/          基线、开源计划、upstream 拆分说明
```

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/compat_patcher.py --target .baseline/hermes --check
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/compat_patcher.py --target overlays/hermes --verify
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target overlays/hermes
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /path/to/hermes
./scripts/verify_no_secrets.sh
git diff --check
```

## Compat Patcher

`scripts/compat_patcher.py` 对 Hermes root 做最小 gateway 补丁，不覆盖整个 `gateway/run.py`。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/compat_patcher.py --target /opt/hermes --check
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/compat_patcher.py --target /opt/hermes --apply
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/compat_patcher.py --target /opt/hermes --verify
```

`--check` 只检查锚点；`--apply` 应用缺失补丁；`--verify` 要求关键结构存在。重复执行 `--apply` 应返回 `changed_count=0`。

## Local Install Chain

`scripts/install_dingtalk_kit.py` 把当前 DingTalk Kit 安装到一个 Hermes root：按声明清单复制 `plugins/platforms/dingtalk/` 和 `plugins/product_confirmation/`，再运行 post-install verifier。每个新插件目录先在目标目录旁边、同一文件系统内完成 staging（预备副本），再用“旧目录改名 → 新目录改名”切换；第二步失败会立即把旧目录改回。默认模式保留已有 compat patcher；任一步失败都会恢复安装前的 gateway 三文件和两个旧插件目录。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /opt/hermes
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /opt/hermes --json
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /opt/hermes --plugins-only --json
```

本地 baseline 临时 root 的默认模式输出 `operation_count=7 failure_count=0`，其中 compat `changed_count=3 failure_count=0`，verifier `check_count=50 failure_count=0`。`--plugins-only` 是升级对账模式：只安装两个 Kit 自有插件目录，报告中的 `core_manifest.changed_paths` 必须为 `[]`；它不会静默删除默认模式里的旧 compat。这个脚本不读取 `.env`，不做真实 DingTalk 网络收发；生产切换需要另开带回滚点的 canary 任务。

## Post-Install Verifier

`scripts/post_install_verifier.py` 对安装后的 Hermes root 做只读验收，确认 gateway compat 结构、DingTalk 插件 manifest、Hermes runtime 可发现 `dingtalk` adapter、`raw_process` ACK、reply context、Product 的 5 个工具与公开 hook，以及 `session_key` slash 和 `session_context` bridge 都存在。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target /opt/hermes
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target /opt/hermes --json
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target /opt/hermes --plugins-only --json
```

本地 overlay 默认模式当前输出 `check_count=50 failure_count=0`，其中 runtime discovery 和 Product public-hook contract 在不含完整 `hermes_cli/` 的 overlay root 上为 `skipped`；完整 Hermes canary root 必须输出 `plugin.runtime_discovery ... runtime_dingtalk_entry=dingtalk plugin=dingtalk-platform`，并且 `product.public_hook_contract` 为 `ok`。这个 verifier 不做真实 DingTalk 网络收发；需要真实消息验收时另开带凭证和脱敏边界的任务。

## 开源前状态

当前仓库先用于冻结和整理。公开前必须满足：

1. `scripts/verify_no_secrets.sh` 通过。
2. `python3 -m unittest discover -s tests` 通过。
3. compat patcher、installer 和 post-install verifier 的门禁已满足。
4. README 明确支持的 Hermes 基础镜像版本。

已提交到 Hermes 官方的拆分 PR。它们是回馈 upstream 的候选，不是安装前提：

- [#58910 raw_process ACK 兼容](https://github.com/NousResearch/hermes-agent/pull/58910)
- [#58913 session_key slash 修复](https://github.com/NousResearch/hermes-agent/pull/58913)
- [#58914 HERMES_SESSION_ID 传递](https://github.com/NousResearch/hermes-agent/pull/58914)
- [#58917 DingTalk replied text 上下文](https://github.com/NousResearch/hermes-agent/pull/58917)
