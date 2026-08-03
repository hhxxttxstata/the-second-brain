"""Chatbot Agent — 两层上下文架构 + 跨会话延续。

流程:
1. gather_context → 合并 agent_data 记忆 + vault 知识 + 待审批操作 → 注入 system prompt
2. llm → tools? → done

改进: 每轮对话重新注入最新的上下文 + 待审批延续（P0 修复）。
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from ..agent_data_service import build_context
from ..trace import TraceSession
from .llm import get_chat_model
from .tools import get_agent_tools

from app.core.logging import logger
from typing import Any

# 增量缓存：只在记忆文件变更时重建上下文
_last_context_ts: str = ""
_last_context_cache: str = ""


def _build_system_prompt(task: str = "", trace: Any = None,
                          session_id: str = "") -> str:
    """构建 system prompt，含分层上下文 + 待审批延续。"""
    global _last_context_ts, _last_context_cache

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
- update_task_status / get_today_state — manage tasks
- get_fund_data / get_github_trending / get_ai_news — external data
- medical_rag_query / medical_pe_diagnosis — medical knowledge QA and PE image diagnosis (when user asks medical questions)

Think naturally, answer naturally. Use tools when helpful.

## Important rules:
- If a tool returns empty or fails, tell the user honestly: say "I couldn't find anything" or "the tool returned an error". Do NOT make up or guess content.
- If you read a file and it has actual content, describe what it says. If the file is empty or doesn't exist, say so. Do NOT claim content is "blank" if it isn't.
- Never claim a tool had "parameter problems" unless you called it and saw an actual error message.
- Base your answers on actual tool results, not on what you assume the vault contains.
- **Memory write precision**: when the user mentions a TEMPORARY state (headache, tiredness, being busy, mood) that only affects THIS conversation, do NOT save it as a long-term preference. Only save STABLE preferences ("以后都", "默认", "习惯用"). A temporary condition like "今天头痛" does not make "头痛时回复简短" a durable preference — at most apply it to the current session without persisting. However, DO still save genuine stable preferences stated in the same message (e.g. "以后所有的代码示例都默认用 Python" IS a durable preference — write it). Save stable preferences, skip temporary states.
- **Always persist stable preferences with a tool call**: when the user states a durable preference ("以后都", "默认", "记住"), ACTUALLY call write_memory / write_topic_memory / search_memories to persist it. Saying "I've remembered it" in text WITHOUT calling the tool is a false completion — never claim you saved something you didn't."""
    # fmt: on


def gather_context_node(state: MessagesState) -> dict:
    """每轮重建 system prompt（缺陷三修复）。"""
    from langchain_core.messages import SystemMessage

    messages = state.get("messages", [])
    last_text = ""
    for m in reversed(messages):
        if hasattr(m, "content") and isinstance(m.content, str):
            last_text = m.content[:200]
            break

    logger.info("chatbot.gather", step="📚 构建上下文（记忆 + vault 知识）...")
    system_prompt = _build_system_prompt(task=last_text)

    # 移除旧的 SystemMessage，替换为最新的
    new_messages = [m for m in messages if not isinstance(m, SystemMessage)]
    prefixed = [SystemMessage(content=system_prompt)] + new_messages
    logger.info("chatbot.gather.done", step=f"✅ 上下文已注入（{len(system_prompt)} chars）")
    return {"messages": prefixed}


def call_model_node(state: MessagesState) -> dict:
    logger.info("chatbot.llm", step="🤖 LLM 思考中...")
    tools = get_agent_tools()
    model = get_chat_model().bind_tools(tools)
    response = model.invoke(state["messages"])
    has_tool_calls = hasattr(response, "tool_calls") and len(response.tool_calls) > 0
    if has_tool_calls:
        tools_str = ", ".join(tc.get("name", "?") for tc in response.tool_calls)
        logger.info("chatbot.llm.tools", step=f"🔧 LLM 调用工具: {tools_str}")
    else:
        logger.info("chatbot.llm.done", step="✅ LLM 回答生成完毕")
    return {"messages": [response]}


def build_chatbot_graph():
    graph = StateGraph(MessagesState)

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

    return graph.compile(checkpointer=MemorySaver())
