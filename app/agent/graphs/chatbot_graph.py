"""Chatbot Agent — 两层上下文架构 + 跨会话延续。

流程:
1. gather_context → 合并 agent_data 记忆 + vault 知识 + 待审批操作 → 更新 system prompt
2. llm → tools? → done

跨轮状态 (2026-08 改造):
- 使用固定 thread_id + SqliteSaver: 同一会话的多次 invoke 自动恢复并累积
  messages 历史, 不再由调用方手工 load_messages 注入。
- system prompt 存独立 state 字段 (不放进 messages):
  避免 add_messages 把历史消息/SystemMessage 重复累积, 保证 LLM 每轮
  只看到 [最新 system] + [不重复的历史]。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from ..agent_data_service import build_context
from ..checkpoint import get_checkpointer
from ..trace import TraceSession
from .llm import get_chat_model
from .tools import get_agent_tools

from app.core.logging import logger


def _chat_messages_reducer(left, right):
    """ChatState.messages 的 reducer：支持整体替换 + 常规追加。

    - 节点返回 {"replace": [...]} → 整体替换累积的消息（用于压力压缩）
    - 其他情况 → 走 add_messages 标准追加/去重语义
    """
    if isinstance(right, dict) and "replace" in right:
        return list(right["replace"])
    return add_messages(left, right)


def _sanitize_messages(messages: list) -> list:
    """修复残缺消息序列：丢弃无前驱 assistant tool_calls 的孤立 ToolMessage。

    来源（2026-08-28 badcase 定位）:
      - 压力压缩的 keep 切片按条数截断，可能把早期 ai(tool_calls) 换成摘要，
        却把其后的 tool 结果留在序列开头 → 孤立 tool；
      - 会话中断残留（ToolNode 写 checkpoint 后进程退出）。
    LLM 对"role=tool 但前驱无 tool_calls"的消息直接返回 400。
    防御性兜底：任何来源的坏序列在调用前都会被修复。
    """
    out: list = []
    pending_calls = 0  # 未配对的 ai tool_calls 数（一条 ai 可含多个 tool_calls）
    for m in messages:
        role = getattr(m, "type", "?")
        if role == "tool":
            if pending_calls <= 0:
                continue  # 孤立 tool：前驱已被压缩/缺失，结果无意义，丢弃
            pending_calls -= 1
            out.append(m)
        else:
            if role == "ai":
                pending_calls = len(getattr(m, "tool_calls", None) or [])
            out.append(m)
    return out


class ChatState(TypedDict):
    """Chatbot 图状态: messages 只累积 human/ai/tool 消息, system 独立存放。

    - messages: 自定义 reducer → 跨调用自动累积, 不重复; 压力压缩时整体替换
    - system: 每轮由 gather_context 重建 (最新的分层上下文)
    """
    messages: Annotated[list, _chat_messages_reducer]
    system: str


def _build_system_prompt(task: str = "", trace: Any = None,
                          session_id: str = "") -> str:
    """构建 system prompt，含分层上下文 + 待审批延续。"""
    ctx = build_context(task=task, session_id=session_id)

    # 追踪 manifest
    manifest = ctx.get("manifest", {})

    # 记录 trace 上下文
    if trace:
        for s in ctx.get("sources", []):
            trace.add_context_source(
                s.get("layer", "?"), s.get("type", "?"),
                len(ctx.get("context", "")),
            )
        # 记录 manifest
        if manifest:
            trace.context_sources.append({
                "layer": "observability",
                "type": "context_manifest",
                "chars": 0,
                "preview": str(manifest)[:200],
            })

    return f"""{ctx['context']}

You are the user's personal AI agent. You have tools to read vault notes, write memory, and query external data.

## Architecture (two layers):
- **vault/** (D:/MYWORLD) — the user's knowledge base (.md notes/diaries). Read+write for you.
- **agent_data/** — your own runtime data (memories, tasks, traces). Read/write as needed.

## Conversation continuity rules:
- If the user says "同意", "好", "就按这个来", "ok", "go ahead", "可以", "yes",
  or otherwise **confirms or agrees** to a proposal YOU made in your previous message
  — **immediately execute that proposal**. Do NOT re-analyze, re-summarize, or ask again.
  The user already approved; just do it.
- If you see a **## 待审批操作** section above: these are actions pending from a
  previous session. If the user agrees, execute them.
- **Vault write rules**: vault_write and vault_append no longer require user approval.
  You are free to read and write vault files as needed.
  But be careful with deletion — prefer to keep original content when in doubt.
  You may rephrase sentences for clarity and logical flow.
- If the user says "不是" / "不对" / "我哪里说过" — they are **disputing** something
  in memory or vault. Read vault and memory to verify before correcting yourself.
- If the user asks "刚才说的什么" / "你记得吗" — refer to the conversation history
  in the messages (preceding this prompt), NOT just the memory layer.
- The **## 系统策略** section above contains binding rules. Follow them before all else.

## Tools available: search_topic_memory, read_topic_memory, write_topic_memory — manage MEMORY.md indexed memories
- search_vault / read_folder / read_file — search the knowledge base
- search_memories — search SQLite memories
- write_episodic_memory / read_memory — manage episodic memories
- delete_memory — forget a memory when the user asks (trust mechanism)
- ask_clarification — ask the user when the request is ambiguous
- update_task_status / get_today_state — manage tasks
- create_handoff / complete_handoff / update_handoff_status — cross-session task continuity (see rules below)
- get_fund_data / get_github_trending / get_ai_news — external data
- generate_excel / control_visio / run_code — programming: Excel reports, Visio flowcharts, Python code execution (when user asks to create tables/diagrams/scripts)
- create_tool — create a NEW tool when existing tools cannot accomplish the task (see rules below)

## Dynamic tool creation (create_tool) rules:
- Trigger ONLY when: the user's task cannot be done with existing tools AND the needed capability is reusable (e.g. a recurring data fetch/calculation), or the user explicitly asks to build a tool.
- **Before calling create_tool, confirm the intent with the user first** (via ask_clarification or plain text): the tool name, what it does, and its parameters. Do NOT silently write tools.
- The `code` is a Python `def handler(**kwargs) -> str` function (or just its body). Available namespace: json/math/re/datetime/vault/ads/requests. Forbidden: open, os/sys imports, print (use return), dunder attributes. Invalid or unsafe code will be rejected with an error — report it honestly and fix it if the user still wants the tool.
- After a successful create_tool, tell the user the tool is ready and call it in the NEXT turn to fulfill their original request (it becomes visible immediately).

## Cross-session task continuity (handoff) rules:
- When a task CANNOT be finished in this session (needs user input, approval, external data, or is simply too long) and MUST continue in a later session → call **create_handoff** with a clear one-sentence goal, what was completed, the pending action, and the next step. The next session automatically loads it into context.
- If you just finished the work a handoff asked for → call **complete_handoff** so the task is removed from the active list. Leaving it active pollutes future sessions.
- If progress was made but the task is still not done → call **update_handoff_status** to keep the handoff accurate.
- Do NOT create a handoff for tasks that will be finished within this session, and do NOT create duplicate handoffs for the same goal — check the active tasks section in context first.

Think naturally, answer naturally. Use tools when helpful.

## Important rules:
- **Ask before guessing**: when the user's request is genuinely ambiguous (unclear references like "那个项目", missing required parameters), call **ask_clarification** to ask first. Do NOT guess and do NOT fabricate context. Only ask when truly ambiguous — don't over-ask for trivial details you can reasonably infer.
- **Forgetting**: when the user says "忘掉/删除我之前说的 X" / "forget X", call **delete_memory** (search for candidates first, then delete by memory_id) — forgetting is part of trust. Do not claim you forgot something without actually deleting it.
- If a tool returns empty or fails, tell the user honestly: say "I couldn't find anything" or "the tool returned an error". Do NOT make up or guess content.
- If you read a file and it has actual content, describe what it says. If the file is empty or doesn't exist, say so. Do NOT claim content is "blank" if it isn't.
- Never claim a tool had "parameter problems" unless you called it and saw an actual error message.
- Base your answers on actual tool results, not on what you assume the vault contains.
- **Memory write precision**: when the user mentions a TEMPORARY state (headache, tiredness, being busy, mood) that only affects THIS conversation, do NOT save it as a long-term preference. Only save STABLE preferences ("以后都", "默认", "习惯用"). A temporary condition like "今天头痛" does not make "头痛时回复简短" a durable preference — at most apply it to the current session without persisting. However, DO still save genuine stable preferences stated in the same message (e.g. "以后所有的代码示例都默认用 Python" IS a durable preference — write it). Save stable preferences, skip temporary states.
- **Always persist stable preferences with a tool call**: when the user states a durable preference ("以后都", "默认", "记住"), ACTUALLY call write_memory / write_topic_memory / search_memories to persist it. Saying "I've remembered it" in text WITHOUT calling the tool is a false completion — never claim you saved something you didn't.
- **SOURCE CITATION (mandatory)**: whenever your answer is based on vault notes, memories, or external tool results, cite the source. Use the file path exactly as returned by the tool (e.g. `来源: notes/项目笔记/agent项目简历描述.md` or `来源: diaries/2026-08-06.md`). Put the citation at the end of the relevant paragraph or answer section. When citing memory, name the memory type (e.g. `来源: episodic记忆` / `来源: 待办`). If you did NOT use any source (pure reasoning/chat), you may omit citations — but never cite a file you did not read. Correct: 我在 `notes/秋招准备/面试八股.md` 里找到相关内容（来源: notes/秋招准备/面试八股.md）。Incorrect: claiming information without naming where it came from when tools were used."""
    # fmt: on


def gather_context_node(state: ChatState) -> dict:
    """每轮重建 system prompt（只更新 system 字段，不触碰累积的 messages）。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages = list(state.get("messages", []))
    last_text = ""
    for m in reversed(messages):
        if hasattr(m, "content") and isinstance(m.content, str):
            last_text = m.content[:200]
            break

    logger.info("chatbot.gather", step="📚 构建上下文（记忆 + vault 知识）...")
    system_prompt = _build_system_prompt(task=last_text)
    logger.info("chatbot.gather.done", step=f"✅ 上下文已注入（{len(system_prompt)} chars）")
    try:
        from ..trace import get_current_trace
        _cur = get_current_trace()
        if _cur is not None:
            _cur.add_step(f"📚 构建上下文（{len(system_prompt)} chars）")
    except Exception:
        pass

    # ── Checkpoint 历史压力压缩 ──
    # checkpoint 累积的 messages 会随轮次无限增长; 超 HISTORY_BUDGET 时把早期
    # 轮次替换为会话摘要, 保留最近 KEEP_RECENT_TURNS 轮原文 (含 tool 消息原文,
    # 维持 tool-call/tool-result 配对)。用 {"replace": ...} 整体重建 messages,
    # 避免 add_messages 追加导致重复。
    from ..context_pressure import (
        HISTORY_BUDGET, KEEP_RECENT_TURNS, _load_session_summaries, estimate_tokens,
    )

    history_tokens = sum(estimate_tokens(str(getattr(m, "content", ""))) for m in messages)
    if history_tokens > HISTORY_BUDGET and len(messages) > KEEP_RECENT_TURNS * 2:
        early = messages[:-KEEP_RECENT_TURNS * 2]
        keep = messages[-KEEP_RECENT_TURNS * 2:]
        summaries = _load_session_summaries(None)
        if summaries:
            summary_text = "[早期会话摘要]\n" + "\n".join(f"- {s[:120]}" for s in summaries[:4])
        else:
            lines = []
            for m in early:
                if isinstance(m, HumanMessage):
                    role = "用户"
                elif isinstance(m, ToolMessage):
                    continue  # 工具消息不进压缩行, 避免冗余
                else:
                    role = "助手"
                text = str(getattr(m, "content", ""))[:60].replace("\n", " ")
                if text:
                    lines.append(f"{role}: {text}")
            summary_text = "[早期会话压缩摘要]\n" + "\n".join(lines[-30:])
        compressed = [AIMessage(content=summary_text)] + keep
        # 压缩后修复配对：keep 切片可能把 tool 结果留在开头而前驱 ai(tool_calls)
        # 已被摘要替代 → 产生孤立 tool 消息（LLM 400），这里统一丢弃
        compressed = _sanitize_messages(compressed)
        logger.info(
            "chatbot.gather.compressed",
            step=f"♻️ 历史压力压缩: {history_tokens} tokens, "
                 f"{len(messages)} → {len(compressed)} 条消息, 保留近 {KEEP_RECENT_TURNS} 轮原文",
        )
        return {"system": system_prompt, "messages": {"replace": compressed}}

    # system 存独立字段：messages 由 checkpointer 累积且不重复
    return {"system": system_prompt}


def call_model_node(state: ChatState) -> dict:
    from langchain_core.messages import SystemMessage

    logger.info("chatbot.llm", step="🤖 LLM 思考中...")
    tools = get_agent_tools()
    model = get_chat_model().bind_tools(tools)
    # 组装 [最新 system] + [checkpoint 累积的历史]；system 不写回 messages
    # sanitize 兜底：checkpoint 中可能残留孤立 tool 消息（压缩截断/会话中断），
    # 直接发给 LLM 会 400，调用前统一修复
    llm_messages = [SystemMessage(content=state.get("system", ""))] + _sanitize_messages(
        list(state.get("messages", [])))

    # 流式上下文（POST /agent/v2/chat/stream）→ 逐 token 推给前端；
    # 普通 invoke 路径（CLI/eval/测试）无 stream writer，行为完全不变。
    # writer 优先读 orchestrator 的线程本地桥（子图自身的 get_stream_writer
    # 绑定子图内部 stream，写入会被丢弃）；桥为空时才尝试自身 context
    response = None
    writer = None
    try:
        from .structured import STREAM_WRITER_BRIDGE
        writer = getattr(STREAM_WRITER_BRIDGE, "writer", None)
    except Exception:
        writer = None
    if writer is None:
        try:
            from langgraph.config import get_stream_writer
            writer = get_stream_writer()
        except Exception:
            writer = None
    if writer is not None:
        try:
            merged = None
            for chunk in model.stream(llm_messages):
                merged = chunk if merged is None else merged + chunk
                delta = getattr(chunk, "content", "")
                if isinstance(delta, str) and delta:
                    writer({"type": "token", "text": delta})
            if merged is not None:
                response = merged  # AIMessageChunk：tool_call_chunks 已合并成 tool_calls
        except Exception:
            response = None  # 流式失败 → 降级一次性 invoke
    if response is None:
        response = model.invoke(llm_messages)
    # 记录 LLM 用量到当前 trace（此前 set_llm_stats 无生产调用者, token 恒为 0）
    try:
        from ..trace import get_current_trace
        cur = get_current_trace()
        if cur is not None:
            usage = getattr(response, "usage_metadata", None) or {}
            cur.set_llm_stats(
                model=getattr(model, "model_name", ""),
                prompt_tokens=int(usage.get("input_tokens", 0) or 0),
                completion_tokens=int(usage.get("output_tokens", 0) or 0),
            )
    except Exception:
        pass
    try:
        from ..trace import get_current_trace
        _cur = get_current_trace()
        if _cur is not None:
            _cur.add_step("🤖 LLM 思考中...")
    except Exception:
        pass
    has_tool_calls = hasattr(response, "tool_calls") and len(response.tool_calls) > 0
    if has_tool_calls:
        tools_str = ", ".join(tc.get("name", "?") for tc in response.tool_calls)
        logger.info("chatbot.llm.tools", step=f"🔧 LLM 调用工具: {tools_str}")
        try:
            if _cur is not None:
                _cur.add_step(f"🔧 LLM 调用工具: {tools_str}")
        except Exception:
            pass
    else:
        logger.info("chatbot.llm.done", step="✅ LLM 回答生成完毕")
    return {"messages": [response]}


def build_chatbot_graph():
    graph = StateGraph(ChatState)

    graph.add_node("gather_context", gather_context_node)
    graph.add_node("llm", call_model_node)
    graph.add_node("tools", ToolNode(get_agent_tools()))

    graph.add_edge(START, "gather_context")
    graph.add_edge("gather_context", "llm")
    graph.add_conditional_edges(
        "llm",
        tools_condition,
        {"tools": "tools", END: END},
    )
    graph.add_edge("tools", "llm")

    return graph.compile(checkpointer=get_checkpointer())
