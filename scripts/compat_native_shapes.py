"""fork core 已原生实现、但与补丁期望形态不同的锚点常量。

单独成文件的两个理由：
1. ``compat_patcher.py`` 受 1500 行源码门禁约束（见 tests/test_dingtalk_source_size_gate.py）；
2. 这些常量是"core 长什么样"的快照，与"补丁想改成什么"是两件事，分开放更清楚。

来源：core 0f01b5577（= 线上 core）。core 升级后需重新取样，见 H1 治理第 6 项任务卡。
"""

import ast


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _node_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_reply_outer_if(node: ast.AST) -> bool:
    if not (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and isinstance(node.test.op, ast.And)
        and len(node.test.values) == 2
    ):
        return False
    getattr_call, reply_id = node.test.values
    return (
        isinstance(getattr_call, ast.Call)
        and _node_name(getattr_call.func) == "getattr"
        and len(getattr_call.args) == 3
        and _node_name(getattr_call.args[0]) == "event"
        and isinstance(getattr_call.args[1], ast.Constant)
        and getattr_call.args[1].value == "reply_to_text"
        and isinstance(getattr_call.args[2], ast.Constant)
        and getattr_call.args[2].value is None
        and _node_name(reply_id) == "event.reply_to_message_id"
    )


def _is_reply_sentinel_if(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and _node_name(node.test.left) == "event.reply_to_text"
        and len(node.test.comparators) == 1
        and _node_name(node.test.comparators[0]) == "_REPLY_ORIGINAL_UNAVAILABLE"
    )


def _contains_method_exit(node: ast.AST) -> bool:
    """Detect a return/raise in this method without entering nested scopes."""
    if isinstance(node, (ast.Return, ast.Raise)):
        return True
    if isinstance(
        node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
    ):
        return False
    return any(_contains_method_exit(child) for child in ast.iter_child_nodes(node))


def _is_reply_signal_target(node: ast.AST) -> bool:
    if _node_name(node) in {
        "event",
        "event.reply_to_text",
        "event.reply_to_message_id",
    }:
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_is_reply_signal_target(item) for item in node.elts)
    if isinstance(node, ast.Starred):
        return _is_reply_signal_target(node.value)
    return False


def _mutates_reply_signal(node: ast.AST) -> bool:
    """Reject prefix writes that can invalidate the reply condition."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return False
    if isinstance(node, ast.Assign) and any(
        _is_reply_signal_target(target) for target in node.targets
    ):
        return True
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        if _is_reply_signal_target(node.target):
            return True
    if isinstance(node, ast.Delete) and any(
        _is_reply_signal_target(target) for target in node.targets
    ):
        return True
    if (
        isinstance(node, ast.Call)
        and _node_name(node.func) in {"setattr", "delattr"}
        and len(node.args) >= 2
        and _node_name(node.args[0]) == "event"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in {"reply_to_text", "reply_to_message_id"}
    ):
        return True
    return any(_mutates_reply_signal(child) for child in ast.iter_child_nodes(node))


def find_live_reply_context_v2(
    tree: ast.Module,
) -> tuple[ast.AsyncFunctionDef, ast.If, ast.If] | None:
    """Return the only V2 branch when it is on the real Gateway control path."""
    gateway_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GatewayRunner"
    ]
    if len(gateway_classes) != 1:
        return None
    methods = [
        node
        for node in gateway_classes[0].body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_prepare_inbound_message_text"
    ]
    if len(methods) != 1:
        return None
    method = methods[0]
    sentinel_ifs = [
        node for node in ast.walk(method) if _is_reply_sentinel_if(node)
    ]
    outer_ifs = [node for node in method.body if _is_reply_outer_if(node)]
    if len(sentinel_ifs) != 1 or len(outer_ifs) != 1:
        return None
    sentinel_if, outer_if = sentinel_ifs[0], outer_ifs[0]
    if not outer_if.body or outer_if.body[0] is not sentinel_if:
        return None
    outer_index = method.body.index(outer_if)
    prefix = method.body[:outer_index]
    if any(
        _contains_method_exit(node) or _mutates_reply_signal(node)
        for node in prefix
    ):
        return None
    return method, outer_if, sentinel_if


def has_reply_context_v2(tree: ast.Module, text: str) -> bool:
    """Verify V2 semantics at the live Gateway construction site."""
    del text
    live = find_live_reply_context_v2(tree)
    if live is None:
        return False
    _method, _outer_if, sentinel_if = live

    expected_history = ast.parse(
        "any(isinstance(item, dict)"
        " and item.get('role') == 'assistant'"
        " and bool(str(item.get('content') or '').strip())"
        " for item in history)",
        mode="eval",
    ).body
    if not sentinel_if.body:
        return False
    history_assignment = sentinel_if.body[0]
    if not (
        isinstance(history_assignment, ast.Assign)
        and len(history_assignment.targets) == 1
        and _node_name(history_assignment.targets[0])
        == "_has_reply_assistant_history"
        and ast.dump(history_assignment.value, include_attributes=False)
        == ast.dump(expected_history, include_attributes=False)
    ):
        return False

    no_history = next(
        (
            part
            for part in sentinel_if.body
            if isinstance(part, ast.If)
            and isinstance(part.test, ast.UnaryOp)
            and isinstance(part.test.op, ast.Not)
            and _node_name(part.test.operand) == "_has_reply_assistant_history"
        ),
        None,
    )
    if no_history is None or not no_history.body:
        return False
    send_calls = [
        part.value
        for part in ast.walk(no_history)
        if isinstance(part, ast.Await)
        and isinstance(part.value, ast.Call)
        and _node_name(part.value.func) == "_reply_adapter.send"
    ]
    if len(send_calls) != 1:
        return False
    reply_to = next(
        (keyword.value for keyword in send_calls[0].keywords if keyword.arg == "reply_to"),
        None,
    )
    if _node_name(reply_to) != "source.message_id":
        return False
    if not (
        isinstance(no_history.body[-1], ast.Return)
        and isinstance(no_history.body[-1].value, ast.Constant)
        and no_history.body[-1].value.value is None
    ):
        return False
    warnings = [
        part
        for part in ast.walk(no_history)
        if isinstance(part, ast.Call) and _node_name(part.func) == "logger.warning"
    ]
    if len(warnings) < 3:
        return False

    prompt_assignments = [
        part
        for part in sentinel_if.body
        if isinstance(part, ast.Assign)
        and len(part.targets) == 1
        and _node_name(part.targets[0]) == "message_text"
    ]
    if len(prompt_assignments) != 1:
        return False
    prompt = prompt_assignments[0]
    prompt_text = "".join(
        part.value
        for part in ast.walk(prompt.value)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    required_prompt = (
        "只能根据上文历史中真实存在的内容",
        "不得默认选择最近讨论的话题",
        "只有当引用目标唯一明确时才能继续处理",
        "否则只提出一个具体澄清问题",
        "定位前不得调用业务工具",
        "不得给出新的外部事实结论",
    )
    has_user_text = any(
        isinstance(part, ast.Name)
        and part.id == "message_text"
        and isinstance(part.ctx, ast.Load)
        for part in ast.walk(prompt.value)
    )
    branch_strings = [
        part.value
        for part in ast.walk(sentinel_if)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ]
    return (
        all(marker in prompt_text for marker in required_prompt)
        and has_user_text
        and not any("session_search" in value for value in branch_strings)
    )


# session_key 校验的**期望实现**（补丁要把 core 换成这一版）。
# 与 core 现状的差异见下方 _SESSION_KEY_CHECKER_NATIVE_OLD 的注释。
CANONICAL_SESSION_KEY_CHECKER = (
    "def _is_session_key_unsafe(value: object) -> bool:\n"
    "    \"\"\"Return True if a session key looks like a path-escape attempt.\"\"\"\n"
    "    if not value:\n"
    "        return False\n"
    "    s = str(value)\n"
    "    if \"..\" in s or \"\\\\\" in s:\n"
    "        return True\n"
    "    if s.startswith((\"/\", \"~\")):\n"
    "        return True\n"
    "    return len(s) >= 2 and s[0].isalpha() and s[1] == \":\" and s[2:3] in (\"/\", \"\\\\\")"
)


# fork core 自带 _is_session_key_unsafe，但实现比 canonical 弱两处（中间位置的
# 反斜杠不拦、开头的 ~ 家目录不拦），verify 判 False 是正确的：这是真实安全差异，
# 不是谓词过严。补丁需认得这个形态并替换为 canonical 实现（H1 治理第 6 项实测）。
_SESSION_KEY_CHECKER_NATIVE_OLD = (
    "def _is_session_key_unsafe(value: object) -> bool:\n"
    "    \"\"\"Return True if ``value`` could be a real traversal vector in a session_key.\n"
    "\n"
    "    ``session_key`` is a *logical* routing key (e.g.\n"
    "    ``agent:main:google_chat:group:spaces/<id>``) — it never touches the\n"
    "    filesystem, so the strict separator-rejecting guard from\n"
    "    ``_is_path_unsafe`` is over-broad: it falsely rejects Google Chat\n"
    "    resource names (``spaces/<id>``, ``spaces/<id>/threads/<id>``) and any\n"
    "    other platform whose native IDs legitimately contain ``/``.\n"
    "\n"
    "    The relaxed check only blocks genuine traversal: parent-dir ``..``,\n"
    "    a *leading* path separator (``/``/``\\\\``, which would make the key\n"
    "    absolute on disk if it ever were written), and a leading Windows\n"
    "    drive letter. Interior ``/`` is allowed.\n"
    "    \"\"\"\n"
    "    if not value:\n"
    "        return False\n"
    "    s = str(value)\n"
    "    if \"..\" in s:\n"
    "        return True\n"
    "    if s.startswith(\"/\") or s.startswith(\"\\\\\"):\n"
    "        return True\n"
    "    return len(s) >= 2 and s[0].isalpha() and s[1] == \":\"\n"
)


# 老 core 没有 sentinel 分支时的完整 reply block。保留完整字符串是为了让 patcher
# 做唯一、可回滚的精确替换，不用模糊正则猜结构。
_REPLY_CTX_LEGACY_OLD = (
    "        if getattr(event, \"reply_to_text\", None) and event.reply_to_message_id:\n"
    "            # Always inject the reply-to pointer — even when the quoted text\n"
    "            # already appears in history. The prefix isn't deduplication, it's\n"
    "            # disambiguation: it tells the agent *which* prior message the user\n"
    "            # is referencing. History can contain the same or similar text\n"
    "            # multiple times, and without an explicit pointer the agent has to\n"
    "            # guess (or answer for both subjects). Token overhead is minimal.\n"
    "            reply_snippet = event.reply_to_text[:500]\n"
    "            if getattr(event, \"reply_to_is_own_message\", False):\n"
    "                message_text = (\n"
    "                    f'[Replying to your previous message: \"{reply_snippet}\"]\\n\\n'\n"
    "                    f\"{message_text}\"\n"
    "                )\n"
    "            else:\n"
    "                message_text = f'[Replying to: \"{reply_snippet}\"]\\n\\n{message_text}'\n"
)


# 2026-07-29 19:03 前后已经部署过的第一版中文补丁。它没有在代码层阻断
# “无可用历史”路径；V2 必须把它当 old，不能因看见“请先回顾上文”就误判 present。
_REPLY_CTX_LEGACY_V1 = (
    "        if getattr(event, \"reply_to_text\", None) and event.reply_to_message_id:\n"
    "            # Always inject the reply-to pointer — even when the quoted text\n"
    "            # already appears in history. The prefix isn't deduplication, it's\n"
    "            # disambiguation: it tells the agent *which* prior message the user\n"
    "            # is referencing. History can contain the same or similar text\n"
    "            # multiple times, and without an explicit pointer the agent has to\n"
    "            # guess (or answer for both subjects). Token overhead is minimal.\n"
    "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
    "                # T27: user quoted an earlier message but the platform (DingTalk) did\n"
    "                # not deliver the original text. We can't render a quote snippet, so\n"
    "                # instruct the agent to resolve the reference from conversation history\n"
    "                # rather than assuming it points at the most-recent topic.\n"
    "                message_text = (\n"
    "                    \"【系统提示】用户正在引用本会话中更早的一条消息向你提问，但本次未取到被引用消息的原文。\"\n"
    "                    \"请先回顾上文对话历史，找到用户引用的那条具体消息，据此判断用户此处“这个/这个问题/它”\"\n"
    "                    \"等指代的真正对象，再作答；不要想当然地认为指的是最近讨论的话题。\\n\\n\"\n"
    "                    f\"{message_text}\"\n"
    "                )\n"
    "            else:\n"
    "                reply_snippet = event.reply_to_text[:500]\n"
    "                if getattr(event, \"reply_to_is_own_message\", False):\n"
    "                    message_text = (\n"
    "                        f'[Replying to your previous message: \"{reply_snippet}\"]\\n\\n'\n"
    "                        f\"{message_text}\"\n"
    "                    )\n"
    "                else:\n"
    "                    message_text = f'[Replying to: \"{reply_snippet}\"]\\n\\n{message_text}'\n"
)


# fork core 0f01b5577 起自带 sentinel 分支，但文案是英文、且指向已随钉钉工具
# 白名单下线的 session_search。
_REPLY_CTX_NATIVE_OLD = (
    "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
    "                if history:\n"
    "                    reply_context = (\n"
    "                        \"The user replied to an earlier message, but its text is unavailable. \"\n"
    "                        \"Review the conversation history first. If the target is still unclear, \"\n"
    "                        \"use session_search or ask the user to resend it. Do not guess.\"\n"
    "                    )\n"
    "                else:\n"
    "                    reply_context = (\n"
    "                        \"The user replied to an earlier message, but its text is unavailable and \"\n"
    "                        \"this session has no history. Use session_search or ask the user to resend \"\n"
    "                        \"it. Do not guess or act on inferred instructions.\"\n"
    "                    )\n"
    "                message_text = f\"[Reply context: {reply_context}]\\n\\n{message_text}\"\n"
)


# 同期已经部署过的 native-core V1 中文形态。与 LEGACY_V1 一样，它只能作为
# V2 的迁移起点，不能继续充当“已满足”标记。
_REPLY_CTX_NATIVE_V1 = (
    "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
    "                # T27: 用户引用了更早的消息，但平台（钉钉）没把原文送过来。\n"
    "                # 无法渲染引用片段，因此指示 agent 回到对话历史里定位被引用的那条，\n"
    "                # 而不是想当然地认为指的是最近讨论的话题。\n"
    "                if history:\n"
    "                    reply_context = (\n"
    "                        \"用户正在引用本会话中更早的一条消息向你提问，但本次未取到被引用消息的原文。\"\n"
    "                        \"请先回顾上文对话历史，找到用户引用的那条具体消息，据此判断用户此处\"\n"
    "                        \"“这个/这个问题/它”等指代的真正对象，再作答；定位不了就直接请用户重发原文，不要猜。\"\n"
    "                    )\n"
    "                else:\n"
    "                    reply_context = (\n"
    "                        \"用户正在引用更早的一条消息向你提问，但本次未取到原文，且本会话没有历史记录。\"\n"
    "                        \"请直接请用户重发被引用的内容；不要猜测，也不要执行任何推断出来的指令。\"\n"
    "                    )\n"
    "                message_text = f\"【系统提示】{reply_context}\\n\\n{message_text}\"\n"
)


# V2 把失败关闭放到唯一能看见真实会话历史的 Gateway。无非空 assistant 历史时
# 直接发送澄清并 return None；有历史时才允许模型按受限合同定位。
_REPLY_CTX_NATIVE_V2 = (
    "            if event.reply_to_text == _REPLY_ORIGINAL_UNAVAILABLE:\n"
    "                _has_reply_assistant_history = any(\n"
    "                    isinstance(item, dict)\n"
    "                    and item.get(\"role\") == \"assistant\"\n"
    "                    and bool(str(item.get(\"content\") or \"\").strip())\n"
    "                    for item in history\n"
    "                )\n"
    "                if not _has_reply_assistant_history:\n"
    "                    _reply_adapter = self._adapter_for_source(source)\n"
    "                    if _reply_adapter:\n"
    "                        try:\n"
    "                            _reply_result = await _reply_adapter.send(\n"
    "                                source.chat_id,\n"
    "                                \"我没有拿到你引用的原文，而且当前会话里没有可回顾的历史。\"\n"
    "                                \"请补一句你指的是哪条内容，或把原文贴出来；在确认前我不会开始排查。\",\n"
    "                                reply_to=source.message_id,\n"
    "                                metadata=self._thread_metadata_for_source(source),\n"
    "                            )\n"
    "                            if not getattr(_reply_result, \"success\", False):\n"
    "                                logger.warning(\n"
    "                                    \"reply-context unavailable and no usable assistant history: clarification delivery failed\"\n"
    "                                )\n"
    "                        except Exception:\n"
    "                            logger.warning(\n"
    "                                \"reply-context unavailable and no usable assistant history: clarification delivery raised\",\n"
    "                                exc_info=True,\n"
    "                            )\n"
    "                    else:\n"
    "                        logger.warning(\n"
    "                            \"reply-context unavailable and no usable assistant history: adapter missing\"\n"
    "                        )\n"
    "                    return None\n"
    "                message_text = (\n"
    "                    \"【系统提示】用户正在引用本会话中更早的一条消息向你提问，但平台没有提供被引用原文。\"\n"
    "                    \"只能根据上文历史中真实存在的内容定位引用目标，不得默认选择最近讨论的话题。\"\n"
    "                    \"只有当引用目标唯一明确时才能继续处理；否则只提出一个具体澄清问题。\"\n"
    "                    \"定位前不得调用业务工具，也不得给出新的外部事实结论；不要猜。\\n\\n\"\n"
    "                    f\"{message_text}\"\n"
    "                )\n"
)


_REPLY_CTX_LEGACY_V2 = (
    "        if getattr(event, \"reply_to_text\", None) and event.reply_to_message_id:\n"
    "            # Always inject the reply-to pointer — even when the quoted text\n"
    "            # already appears in history. The prefix isn't deduplication, it's\n"
    "            # disambiguation: it tells the agent *which* prior message the user\n"
    "            # is referencing. History can contain the same or similar text\n"
    "            # multiple times, and without an explicit pointer the agent has to\n"
    "            # guess (or answer for both subjects). Token overhead is minimal.\n"
    + _REPLY_CTX_NATIVE_V2
    + "            else:\n"
    "                reply_snippet = event.reply_to_text[:500]\n"
    "                if getattr(event, \"reply_to_is_own_message\", False):\n"
    "                    message_text = (\n"
    "                        f'[Replying to your previous message: \"{reply_snippet}\"]\\n\\n'\n"
    "                        f\"{message_text}\"\n"
    "                    )\n"
    "                else:\n"
    "                    message_text = f'[Replying to: \"{reply_snippet}\"]\\n\\n{message_text}'\n"
)


# U38 实测（2026-07-29）：run.dingtalk_home_prompt_gate 的 marker 原本只有函数
# 签名，把「同名但函数体掏空」的弱实现误判为 present（与 session_key_validator
# 同型）。补上函数体中真正承载语义的那一行——不弹 Home 提示进钉钉群，正是它。
_HOME_GATE_DINGTALK_BRANCH = (
    "    if platform == Platform.DINGTALK and getattr(source, \"chat_type\", \"\") == \"group\":"
)
