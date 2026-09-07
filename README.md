# Hermes DingTalk Kit

Hermes DingTalk Kit 是独立维护的 Hermes DingTalk adapter（适配器）与 Product Confirmation（产品确认）源码项目，用来把平台修改先固化为可审阅 Git 提交，再通过安装器进入 Hermes。

当前状态：`PUBLIC / SOURCE_COMPLETE / RELEASE_READY`。**legacy compat 的两个 `ADAPT_REQUIRED` 锚点已于 2026-07-29 解除**（`run.reply_sentinel_constant` 在当前 core 上自愈；`session.path_sensitive_validation` 通过 `Step.native_marker` 识别 core 的等价实现形态解决），12 个补丁在 core `0f01b5577` 上 apply/verify 全绿且幂等，默认 legacy-compat 模式已可发布。详见 H1 治理第 6 项任务卡 `T6_RELEASE_CHAIN_FIX_20260729.md`。

## 包含内容

- `raw_process` / `AckMessage` 兼容：修复 `dingtalk-stream 0.24.3` 调用 `handler.raw_process(msg)` 时官方 adapter 不响应的问题。
- 引用上下文注入：把钉钉 `repliedMsg` 转成 Hermes `reply_to_message_id` / `reply_to_text`，避免群里“这个问题”错绑到最近话题。
- 重启不失忆：放行 DingTalk base64 conversation id 中的内部 `/`，同时保留 `session_id` 的严格路径校验。
- 会话环境修复：向 `set_session_vars(...)` 补传 `session_id`，让插件稳定读取 `HERMES_SESSION_ID`。
- 群内 @ 元信息：从钉钉结构化 `atUsers` 还原“本消息还 @ 了谁”，并在发送侧用 `at_user_ids` 生成结构化 @。
- 发送结果校验：HTTP 200 但响应体 `errcode != 0` 时按失败处理，不再把“机器人已移出群”等拒绝误报成成功。
- Product Confirmation（产品确认）插件：5 个窄工具维护 `PRODUCT_DRAFT -> WAITING_PRODUCT_CONFIRMATION -> PRODUCT_APPROVED/PRODUCT_NEEDS_REVISION -> TECH_DESIGN`；状态和发送 claim（占用权）存 SQLite，同一 `task_id + proposal_version` 并发时只有 claim 获得者可以发送。明确未送达会标为 `FAILED` 后允许重试，结果不确定则保留 `CLAIMED` 并阻止自动重发；身份来自公开 `pre_gateway_dispatch` hook 的 `event.source.user_id_alt`。

## 引用原文保留与超长处理

成功送达的卡片和 webhook 原文保存在 `DINGTALK_KIT_STATE_DIR`，未指定时使用 `$HERMES_HOME/dingtalk-kit/dingtalk_card_replies.db`。沿用既有 SQLite 表结构，旧库可直接打开；正文不再按条数淘汰，也不在 20000 字处截断。数据目录必须使用持久挂载并纳入业务数据备份，磁盘空间仍是实际容量边界；写入失败通过日志及 `reply_context_saved=false` 返回，不能当作已保存。`CardReplyStore(max_rows=...)` 为兼容旧调用保留，但仅限制短期确认，不再限制原文条数。

准确消息编号只在同一聊天内恢复原文；仅有发送时间时只能展示唯一候选，由原请求者确认后继续。完整候选及原请求按每段最多 12000 UTF-8 字节顺序发送（另加段号说明，小于既有 20000 字发送长度），确认命令最后发送；任一段失败即撤销该令牌。令牌保持 10 分钟、同聊天同用户、单次消费；过期和数量清理不会删除正文。

单次引用的原文与请求合计超过 **120000 UTF-8 字节**时，插件明确提示分段，并在任务绑定及模型调用前停止；完整原文仍保留。这是本产品为限制一次核对消息量及模型输入体积设定的保守上限，并非钉钉或模型官方限制。上限内超过 500 字的原文另作为引用资料完整传入，避免 Hermes 核心的引用预览裁剪影响正文。出站 webhook 与卡片 SDK 请求同样移除静默截断；若平台拒绝过大的发送，按真实失败返回，不保存伪造的送达记录。修复前已经丢弃或未保存的原文无法由新版本凭空补回。

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
overlays/      Kit 自有插件源码；既有 gateway compat 仅作分开对账
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

`scripts/install_dingtalk_kit.py` 把当前 DingTalk Kit 安装到一个 Hermes root：按声明清单复制 `plugins/platforms/dingtalk/`、`plugins/product_confirmation/` 和 `plugins/h1_task_write/`，再运行 post-install verifier。每个新插件目录先在目标目录旁边、同一文件系统内完成 staging（预备副本），再用“旧目录改名 → 新目录改名”切换；第二步失败会立即把旧目录改回。默认模式保留已有 compat patcher；任一步失败都会恢复安装前的 gateway 三文件和三个旧插件目录。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /opt/hermes
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /opt/hermes --json
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/install_dingtalk_kit.py --target /opt/hermes --plugins-only --json
```

**默认 legacy-compat 模式即为发布模式**：在 core `0f01b5577` 上实测 `installation_mode=legacy-compat`、`failure_count=0`，compat 12 步 apply 全绿（present 2 / changed 10），再次 apply 为 `changed_count=0`（幂等）。`--plugins-only` 保留为**应急出口**——core 升级导致锚点漂移时可临时跳过补丁，但它同时会静音 verifier 的 compat 段（历史上造成过假绿），**用它发布前必须先补跑 `compat_patcher --verify`**。脚本不读取 `.env`，不做真实 DingTalk 网络收发；生产切换属于另行授权的 H1 发布任务。

## Post-Install Verifier

`scripts/post_install_verifier.py` 对安装后的 Hermes root 做只读验收，确认 gateway compat 结构、DingTalk 插件 manifest、Hermes runtime 可发现 `dingtalk` adapter、`raw_process` ACK，以及引用缺原文时的分层合同：adapter 透传 sentinel；Gateway 有非空 assistant 历史才注入严格定位提示，无可用历史则固定澄清并在模型前停止。它还验证 Product 的 5 个工具与公开 hook、`session_key` slash 和 `session_context` bridge。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target /opt/hermes
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target /opt/hermes --json
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/post_install_verifier.py --target /opt/hermes --plugins-only --json
```

本地 overlay 默认模式当前输出 `check_count=62 failure_count=0`（含 compat 段 13 条 = 12 个 Step 各 1 条 + 组级 `compat.verify` 汇总 1 条；去静音前该段为 0 条），其中 **3 条**在只有 overlay、没有完整 Hermes 运行时的 root 上为 `skipped`：`plugin.build_source_signature`（缺 `gateway/platforms/base.py`）、`plugin.runtime_discovery`（缺 Hermes runtime 模块）、`product.public_hook_contract`（缺公开 hook 运行时）；完整 Hermes canary root 必须输出 `plugin.runtime_discovery ... runtime_dingtalk_entry=dingtalk plugin=dingtalk-platform`、`gateway.reply_context_layering ... ok`，并且 `product.public_hook_contract` 为 `ok`。这个 verifier 不做真实 DingTalk 网络收发；需要真实消息验收时另开带凭证和脱敏边界的任务。

## 发布状态

Product/mention 源码发布满足：

1. `scripts/verify_no_secrets.sh` 通过。
2. `python3 -m unittest discover -s tests` 通过。
3. 默认（legacy-compat）installer 与 post-install verifier 门禁已满足，Product/mention 增量核心 diff 为零。
4. legacy compat 两个 `ADAPT_REQUIRED` 锚点已解除，compat 12 步 apply/verify 全绿且幂等，verifier compat 段已去静音（13 条 check 真实产出）。

已提交到 Hermes 官方的拆分 PR。它们是回馈 upstream 的候选，不是安装前提：

- [#58910 raw_process ACK 兼容](https://github.com/NousResearch/hermes-agent/pull/58910)
- [#58913 session_key slash 修复](https://github.com/NousResearch/hermes-agent/pull/58913)
- [#58914 HERMES_SESSION_ID 传递](https://github.com/NousResearch/hermes-agent/pull/58914)
- [#58917 DingTalk replied text 上下文](https://github.com/NousResearch/hermes-agent/pull/58917)
