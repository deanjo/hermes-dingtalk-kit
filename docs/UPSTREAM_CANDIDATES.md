# Upstream Candidates

## 适合提交到 Hermes 官方的改动

1. DingTalk `raw_process` 兼容。
   证据：`patches/dingtalk-adapter.patch` 新增 `async def raw_process`，对应现有覆盖文件 `overlays/hermes/plugins/platforms/dingtalk/adapter.py:1872`。
   上游 PR：[NousResearch/hermes-agent#58910](https://github.com/NousResearch/hermes-agent/pull/58910)。

2. DingTalk 引用上下文注入。
   证据：`patches/dingtalk-adapter.patch` 新增 `reply_to_message_id` / `reply_to_text`；`patches/gateway-run.patch` 新增 `_REPLY_ORIGINAL_UNAVAILABLE` 分支和中文回溯提示。
   已拆分的上游 PR：[NousResearch/hermes-agent#58917](https://github.com/NousResearch/hermes-agent/pull/58917) 只提交“钉钉回调包含原文时保留 `reply_to_text`”这一半；`_REPLY_ORIGINAL_UNAVAILABLE` 回溯提示暂不提交。

3. `session_key` slash 修复。
   证据：`patches/gateway-session.patch` 新增 `_is_session_key_unsafe`，只对 `session_id` 保留 `_is_path_unsafe`。
   上游 PR：[NousResearch/hermes-agent#58913](https://github.com/NousResearch/hermes-agent/pull/58913)。

4. `HERMES_SESSION_ID` 传递修复。
   证据：`patches/gateway-run.patch` 在 `_set_session_env` 调用里补 `session_id=context.session_id`。
   上游 PR：[NousResearch/hermes-agent#58914](https://github.com/NousResearch/hermes-agent/pull/58914)。

5. `session_context.py` 桥接。
   证据：`patches/gateway-session-context.patch` 是 Hermes-3 当前运行所需桥接；上游提交前要先确认官方主线是否已有等价实现。

## 不适合直接 upstream 的改动

1. 云隐官 / Discourse 社区运营工具。
   原因：它依赖具体社区运营流程，不是 Hermes DingTalk adapter 的通用 bug。

2. Dockerfile 中的浏览器、搜索、运行时工具增强。
   原因：它是部署环境便利项，不是 DingTalk adapter 最小修复。

## 建议 PR 顺序

1. 已提交：最小 `raw_process` 兼容 PR（#58910）。
2. 已提交：`session_key` slash 修复 PR（#58913）。
3. 已提交：`HERMES_SESSION_ID` 传递修复 PR（#58914）。
4. 已提交：DingTalk 有原文引用上下文 PR（#58917）。
5. 暂缓：`_REPLY_ORIGINAL_UNAVAILABLE` 回溯提示和 `session_context.py` chat_type/user_id_alt 桥接；这两项 review 面更大，应等前面 PR 反馈后再拆。
