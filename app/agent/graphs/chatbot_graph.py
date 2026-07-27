"""Chatbot Agent — 两层上下文架构。

流程:
1. gather_context → 合并 agent_data 记忆 + vault 知识 → 注入 system prompt
2. llm → tools? → done

改进: 每轮对话重新注入最新的上下文（缺陷三修复），而不是复用旧的 SystemMessage。
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


def _build_system_prompt(task: str = "", trace: Any = None) -> str:
    """构建 system prompt，含最新上下文。"""
    global _last_context_ts, _last_context_cache

    ctx = build_context(task=task)

    # 记录 trace 上下文
    if trace:
        for s in ctx.get("sources", []):
            trace.add_context_source(
                s.get("layer", "?"), s.get("type", "?"),
                len(ctx.get("context", "")),
            )

    return f"""{ctx['context']}

You are the user's personal AI agent. You have tools to read vault notes, write memory, and query external data.

## Architecture (two layers):
- **vault/** (D:/MYWORLD) — the user's knowledge base (.md notes/diaries). Read-only for you.
- **agent_data/** — your own runtime data (memories, tasks, traces). Read/write as needed.

## Tools available:
- search_vault / read_folder / read_file — search the knowledge base
- write_episodic_memory / read_memory — manage memories
- update_task_status / get_today_state — manage tasks
- get_fund_data / get_github_trending / get_ai_news — external data

Think naturally, answer naturally. Use tools when helpful."""
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
