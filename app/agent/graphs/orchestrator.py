"""Orchestrator — 多 Agent 编排。两层架构。

支持:
- chatbot → 一般对话（默认）
- plan → 每日计划（vault 只读 + agent_data 读写）
- reflect → 反思分析（vault 只读 + agent_data 读写）
- memory → 记忆管理（写 agent_data/memory/）
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.core.logging import logger

from ..trace import TraceRecord, get_trace_stats
from .llm import get_chat_model
from .plan_graph import run_plan_graph
from .reflect_graph import run_reflect
from .memory_graph import run_memory_agent

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class OrchestratorState(TypedDict):
    user_id: str
    input_text: str          # 用户原始输入
    conversation: list       # 历史对话摘要

    route: str               # supervisor 选择的路径
    route_reason: str        # 路由理由

    result: str              # 子 agent 的执行结果
    result_data: dict        # 结构化结果

    success: bool
    error: str | None


# ---------------------------------------------------------------------------
# Supervisor prompt
# ---------------------------------------------------------------------------

SUPERVISOR_PROMPT = """You are the orchestrator of a personal knowledge agent system. Analyze the user's
input and route it to the correct sub-agent.

## Available agents:
1. **chatbot** — general conversation, Q&A, casual chat (default)
2. **plan** — "generate daily plan", "what should I do today", "今日计划"
3. **reflect** — "reflect on this", "analyze", "critique", "帮我分析", "反思"
4. **memory** — "remember", "don't forget", "save this", "write memory", "记忆"

## User input:
{input}

## Conversation history:
{conversation}

## Output — JSON only:
{{
  "route": "chatbot|plan|reflect|memory",
  "reason": "one sentence explanation",
  "extracted_params": {{}}
}}
"""


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def supervisor_node(state: OrchestratorState) -> OrchestratorState:
    """LLM 决定路由。"""
    conv_text = "\n".join(state.get("conversation", [])[-5:]) or "(none)"

    prompt = SUPERVISOR_PROMPT.format(
        input=state.get("input_text", ""),
        conversation=conv_text,
    )
    model = get_chat_model(temperature=0.2)
    try:
        response = model.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        if text.startswith("```"):
            import re
            text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("` \n")
        data = json.loads(text)
        state["route"] = data.get("route", "chatbot")
        state["route_reason"] = data.get("reason", "")
    except Exception:
        state["route"] = "chatbot"
        state["route_reason"] = "fallback: parse error"
    return state


def execute_agent(state: OrchestratorState) -> OrchestratorState:
    """执行选中的子 Agent，带 Trace。"""
    route = state.get("route", "chatbot")
    user_id = state.get("user_id", "default_user")
    text = state.get("input_text", "")

    # 创建 Trace
    trace = TraceRecord(route, text[:100])

    try:
        trace.start()

        if route == "plan":
            r = run_plan_graph(user_id=user_id)
            items = r.get("items", [])
            state["result_data"] = r
            if r.get("success"):
                lines = [f"📋 今日计划 ({r.get('date', '')})\n"]
                for i, item in enumerate(items, 1):
                    src = item.get("source", "")
                    icon = {"diary_todo": "📓", "pending_task": "🔄", "signal": "📡",
                            "stable_profile": "🎯", "default": "•"}.get(src, "•")
                    lines.append(f"  {i}. {icon} [{item.get('priority', 'MEDIUM')}] {item.get('title', '')}")
                state["result"] = "\n".join(lines)
            else:
                state["result"] = f"❌ 生成计划失败: {r.get('error', '')}"

        elif route == "reflect":
            r = run_reflect(subject="diary_or_note", content=text, user_id=user_id)
            state["result_data"] = r
            if r.get("success"):
                parts = [
                    f"🔍 分析:\n{r.get('analysis', '')}\n",
                    f"💡 批判:\n{r.get('critique', '')}\n",
                    f"📌 建议:\n{r.get('suggestions', '')}\n",
                    f"📝 总结:\n{r.get('summary', '')}",
                ]
                state["result"] = "\n".join(parts)
            else:
                state["result"] = f"❌ 反思失败: {r.get('error', '')}"

        elif route == "memory":
            r = run_memory_agent(trigger_text=text, user_id=user_id)
            state["result_data"] = r
            state["result"] = r.get("summary", "记忆处理完成")
            if r.get("decision") in ("write", "update"):
                trace.add_memory_update("episodic", state["result"][:100])

        else:  # chatbot
            # 用 chatbot_graph 运行对话
            from .chatbot_graph import build_chatbot_graph
            from langchain_core.messages import HumanMessage

            graph = build_chatbot_graph()
            # 用 orchestrator 的 thread_id 加后缀，保证连续对话共享同一 checkpoint
            chat_thread = f"orch_chat_{state.get('run_id', uuid.uuid4().hex[:10])}"
            config = {"configurable": {"thread_id": chat_thread}}
            response = graph.invoke(
                {"messages": [HumanMessage(content=text)]},
                config,
            )
            last_msg = response["messages"][-1]
            state["result"] = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
            state["result_data"] = {"message_count": len(response["messages"])}

        state["success"] = True
        trace.final_output = state.get("result", "")[:500]
        trace.success = True

    except Exception as exc:
        logger.error("orchestrator_exec_failed", route=route, error=str(exc))
        state["result"] = f"❌ 执行 {route} 时出错: {str(exc)}"
        state["error"] = str(exc)
        state["success"] = False
        trace.success = False
        trace.error = str(exc)

    finally:
        trace.stop()
        trace.save()

    return state


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

_graph = None


def build_orchestrator():
    global _graph
    if _graph is not None:
        return _graph

    builder = StateGraph(OrchestratorState)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("execute", execute_agent)

    builder.add_edge(START, "supervisor")
    builder.add_edge("supervisor", "execute")
    builder.add_edge("execute", END)

    _graph = builder.compile(checkpointer=MemorySaver())
    return _graph


def run_orchestrator(input_text: str,
                     user_id: str = "default_user",
                     conversation: list | None = None,
                     thread_id: str | None = None) -> dict[str, Any]:
    """运行编排器，自动路由到正确的 Agent。

    Args:
        thread_id: 对话线程 ID。同一 thread_id 的多轮调用共享对话历史。
    """
    start = time.monotonic()

    graph = build_orchestrator()
    initial = {
        "user_id": user_id,
        "input_text": input_text,
        "conversation": conversation or [],
        "route": "",
        "route_reason": "",
        "result": "",
        "result_data": {},
        "success": True,
        "error": None,
    }

    try:
        run_id = thread_id or f"orch_{uuid.uuid4().hex[:10]}"
        result = graph.invoke(initial, {"configurable": {"thread_id": run_id}})
        latency = int((time.monotonic() - start) * 1000)
        return {
            "success": result.get("success", False),
            "route": result.get("route", "?"),
            "route_reason": result.get("route_reason", ""),
            "result": result.get("result", ""),
            "result_data": result.get("result_data", {}),
            "latency_ms": latency,
        }
    except Exception as exc:
        logger.error("orchestrator_failed", error=str(exc))
        return {"success": False, "error": str(exc), "route": "error", "result": str(exc)}
