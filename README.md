# Hermes DingTalk Kit

Hermes DingTalk Kit 是 Hermes 官方 DingTalk adapter（适配器）的生产补丁包，用来减少接入钉钉时反复踩的坑。

当前状态：private staging（私有整理仓库）。代码基于 `hermes-agent:stable-daocloud` 验证；公开发布前还要完成脱敏、测试和 upstream（上游）拆 PR。

## 包含内容

- `raw_process` / `AckMessage` 兼容：修复 `dingtalk-stream 0.24.3` 调用 `handler.raw_process(msg)` 时官方 adapter 不响应的问题。
- 引用上下文注入：把钉钉 `repliedMsg` 转成 Hermes `reply_to_message_id` / `reply_to_text`，避免群里“这个问题”错绑到最近话题。
- 重启不失忆：放行 DingTalk base64 conversation id 中的内部 `/`，同时保留 `session_id` 的严格路径校验。
- 会话环境修复：向 `set_session_vars(...)` 补传 `session_id`，让插件稳定读取 `HERMES_SESSION_ID`。

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
python3 -m unittest discover -s tests
./scripts/verify_no_secrets.sh
```

## 开源前状态

当前仓库先用于冻结和整理。公开前必须满足：

1. `scripts/verify_no_secrets.sh` 通过。
2. `python3 -m unittest discover -s tests` 通过。
3. `patches/` 被拆成可读的 upstream PR 候选。
4. README 明确支持的 Hermes 基础镜像版本。

