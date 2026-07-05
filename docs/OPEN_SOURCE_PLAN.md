# Hermes DingTalk Kit Open Source Plan

## 结论

先做一个干净的 `hermes-dingtalk-kit`，不要直接开源 Hermes-3 现有运行目录。

当前 Hermes-3 运行目录是生产补丁上下文，不是发布包：远程 `/Users/cicada/hermes-docker/hermes-3/image-patches/dingtalk-raw-process` 在 `maxdepth 2` 下有 63 个文件，其中 `.bak/.bad/.pristine` 备份文件 48 个，另有 3 个备份目录。开源前必须先冻结基线、归档历史备份、拆出通用补丁和可选业务插件。

## 当前事实

远程 Hermes-3 的 patched image（补丁镜像）从官方镜像派生：`Dockerfile:1` 是 `FROM hermes-agent:stable-daocloud`，并在 `Dockerfile:15-27` 覆盖 `session_context.py`、`run.py`、`adapter.py`、`yunyin_discourse`、`session.py`。

当前应冻结的 7 个入口文件及 sha256：

| 文件 | sha256 |
|---|---|
| `adapter.py` | `c2afccc75ddda63e1b89480c79ddd44f4118a5c91c8a0e8a5193e67e87e86613` |
| `run.py` | `82f206a2446e0401434dd28dabaafc5b9ba45d5cbe194200ba31040027be00fc` |
| `session.py` | `948a86ac483a63668a7b6990e5107778345b0415190c5bf72796dfb100217cc9` |
| `session_context.py` | `bbea82bea958c1c336647455aa2c84fd0119204bbf2af38558d7ce72bcfc404c` |
| `yunyin_discourse/plugin.yaml` | `e5917e8f86811b750e49370d4b7956b4f57f1bd0f219434d4339465293e9eefd` |
| `yunyin_discourse/__init__.py` | `979553cb33d23ec7d9b911c16fcd1df8c7a9342ee986851412c049df1730f325` |
| `Dockerfile` | `593f0731c646c13e36e9daa65a61d1eaf836d23ce1bda31a80ba2520381617cc` |

本地社区仓库 `/Users/cicada/SourceCode/ai-community/discourse` 里没有云隐官插件：`rg "yunyin|云隐官"` 命中 0 个文件。它有另一个钉钉插件 `plugins-extra/discourse-dingtalk-sso/plugin.rb:3-4`，用途是 DingTalk SSO（钉钉单点登录），不是 Hermes 发帖工具。

## 不冲突边界

`discourse-dingtalk-sso` 是 Discourse 侧插件：`plugins-extra/discourse-dingtalk-sso/plugin.rb:21-26` 注册 `auth_provider`，目标是让用户用钉钉账号登录社区。

`yunyin_discourse` 是 Hermes 侧插件：远程 `yunyin_discourse/plugin.yaml:7-20` 注册 14 个 Hermes 工具，包括 `yunyin_publish_topic`、`yunyin_search_posts`、`yunyin_send_group_message`、`yunyin_manage_notification_config`。

两个插件运行在不同进程和不同插件系统里：

```text
Discourse server
  -> discourse-dingtalk-sso
  -> 登录 / 用户身份

Hermes-3 gateway
  -> yunyin_discourse
  -> 钉钉会话里调用 Discourse API 发帖 / 查帖 / 通知
```

## 拆分策略

### A. 通用包：`hermes-dingtalk-kit`

这部分面向所有接 DingTalk 的 Hermes 用户。

包含：
- DingTalk SDK `raw_process` / `AckMessage` 兼容；远程 `adapter.py:1872-1886` 是现有实现。
- DingTalk 引用消息上下文注入；远程 `adapter.py:992-1021` 填 `reply_to_message_id/reply_to_text`，远程 `run.py:9174-9200` 注入模型输入。
- `session_key` 含 `/` 重启失忆修复；文档 `docs/hermes3-yunyin-v1-loop_20260626/tasks/T10_session_key_slash_amnesia_fix_20260702.md:7` 记录 14 个钉钉路由 key 中 10 个含 `/`。
- `HERMES_SESSION_ID` 传递修复；远程 `run.py:13244-13247` 补 `session_id=context.session_id`。

不包含：
- 商越 AI 社区默认文案。
- `云隐官` 人设。
- Discourse 业务发帖策略。
- 任何 `.env`、API key、token、cookie、Authorization header。

### B. 可选业务插件：`yunyin_discourse`

这部分只给需要“钉钉到 Discourse 社区运营”的团队使用。

开源前必须参数化：
- 默认机器人名：当前文档使用 `云隐官`，见 `docs/hermes3-yunyin-v1-loop_20260626/01_canonical_scope.md:5`。
- 默认社区名称：当前文档使用 `商越 AI 社区`，见 `docs/hermes3-yunyin-v1-loop_20260626/01_canonical_scope.md:9`。
- 默认站点 URL：当前社区文档引用 `https://nexus.sunyur.com/`，见 `/Users/cicada/SourceCode/ai-community/discourse/plugins/discourse-ai/docs/post-content-rag-search/04_live_e2e_plan.md:14`。
- 默认排除群：远程 `yunyin_discourse/__init__.py:90` 当前是 `商越官方群`。

## Upstream 候选

适合提给 Hermes 官方上游的改动：

1. `raw_process` 兼容层：现有 README 记录 `dingtalk-stream 0.24.3` 调 `handler.raw_process(msg)`，而官方 adapter 只有 `process()`；远程 README `image-patches/dingtalk-raw-process/README.md:6-12` 还记录了 issue / PR 链接。
2. DingTalk reply context（引用上下文）注入：这是平台语义修复，不依赖云隐官业务。
3. `session_key` 校验过宽修复：这是通用会话恢复 bug，不依赖 DingTalk 以外的业务。
4. `_set_session_env` 补 `session_id`：这是框架 session context bug，不依赖 Discourse。

不适合直接提 upstream 的改动：

1. `yunyin_discourse` 发帖、查帖、通知工具：这是商越 AI 社区业务流。
2. `group_push_admins`、通知轮询、作者映射等策略：它们依赖公司权限模型。
3. Dockerfile 里的 `ddgs/playwright` 安装：这是 Hermes-3 当前环境增强，不是 DingTalk adapter 的最小修复。

## 清理计划

先归档，再删除构建目录里的历史文件。

建议命令形态：

```bash
cd /Users/cicada/hermes-docker/hermes-3
mkdir -p archives
tar -czf archives/dingtalk-raw-process-backups-20260705.tar.gz \
  $(find image-patches/dingtalk-raw-process -maxdepth 2 \
    \( -name "*.bak*" -o -name "*.bad*" -o -name "*.pristine*" -o -name "__pycache__" -o -name "yunyin_discourse.bak*" \))
shasum -a 256 archives/dingtalk-raw-process-backups-20260705.tar.gz
```

停止条件：另一个会话还在改 Hermes DingTalk 代码时，不做删除；必须先重新跑 `shasum -a 256` 确认 7 个入口文件没有变化。

## 仓库草案

建议仓库名：`hermes-dingtalk-kit`。

目录结构：

```text
hermes-dingtalk-kit/
  README.md
  LICENSE
  docker/
    Dockerfile
  patches/
    dingtalk-adapter.patch
    gateway-session-key.patch
    gateway-session-env.patch
  plugins/
    yunyin_discourse/
      plugin.yaml
      __init__.py
      README.md
  tests/
    test_raw_process_ack.py
    test_session_key_slash.py
    test_reply_context.py
    test_session_env.py
  docs/
    UPSTREAM_CANDIDATES.md
    SECURITY.md
```

同事最低使用方式：

```bash
docker build \
  --build-arg HERMES_BASE=hermes-agent:stable-daocloud \
  -t hermes-agent:dingtalk-kit \
  -f docker/Dockerfile .
```

## 验收标准

开源前至少满足：

1. `find . -name "*.bak*" -o -name "*.bad*" -o -name "*.pristine*"` 输出为空。
2. `rg -n "DISCOURSE_API_KEY|DINGTALK_CLIENT_SECRET|Authorization|cookie|token=" .` 没有真实 secret；允许文档里的变量名示例。
3. `rg -n "商越|云隐官|nexus.sunyur.com|胡东光|192\\.168\\.88\\.33|cicada@" .` 对通用包输出为空；可选业务插件只允许配置示例中出现占位值。
4. 4 个最小测试通过：`raw_process` ack、reply context、session key slash、session env。
5. README 明确版本边界：基于 `hermes-agent:stable-daocloud` 验证；其他 Hermes 版本需先跑兼容检查。

## 下一步

1. 等另一个会话完成 DingTalk 相关修改。
2. 重新采集远程 7 个入口文件的 sha256；如果和本文件基线不同，先更新本计划。
3. 归档并移出 48 个备份文件和 3 个备份目录。
4. 新建干净本地仓库 `hermes-dingtalk-kit`，只复制当前入口文件和必要文档。
5. 把通用修复拆成 upstream PR 候选，把业务插件留作可选模块。
