"""Memory Agent — 写 agent_data/memory/ JSON 文件，不写 vault。"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.core.logging import logger
from ..agent_data_service import (
    add_episodic,
    build_context,
    read_memory,
    save_trace,
    write_memory,
)
from .llm import get_chat_model


class MemoryAgentState(TypedDict):
    user_id: str
    trigger_text: str
    decision: str
    decision_reason: str
    target_type: str
    content: str
    summary: str
    success: bool
    error: str | None


DECIDE_PROMPT = """Evaluate this input for memory-worthiness.

## Input:
{trigger_text}

## User context:
{context}

## Decision: "write" (worth remembering) | "skip" (trivial/chat)

## Output — JSON:
{{"decision":"write|skip","reason":"why","type":"episodic|task","content_memory":"the key info to remember in 1-2 sentences"}}
"""


def decide_node(state: MemoryAgentState) -> MemoryAgentState:
    ctx = build_context(task=state.get("trigger_text", ""), max_tokens=1000)
    prompt = DECIDE_PROMPT.format(
        trigger_text=state.get("trigger_text", ""),
        context=ctx["context"][:2000],
    )
    model = get_chat_model(temperature=0.3)
    try:
        response = model.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        if text.startswith("```"):
            import re
            text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("` \n")
        data = json.loads(text)
        state["decision"] = data.get("decision", "skip")
        state["decision_reason"] = data.get("reason", "")
        state["target_type"] = data.get("type", "episodic")
        state["content"] = data.get("content_memory", state["trigger_text"])
    except Exception:
        state["decision"] = "skip"
    return state


def write_node(state: MemoryAgentState) -> MemoryAgentState:
    if state.get("decision") not in ("write", "update"):
        return {**state, "summary": "(skipped)", "success": True}

    mtype = state.get("target_type", "episodic")
    content = state.get("content", state.get("trigger_text", ""))[:500]

    if mtype == "episodic":
        add_episodic(content, tags=["auto"])
        state["summary"] = f"📝 Agent 记忆已保存 (episodic)"
    elif mtype == "task":
        task_data = read_memory("task")
        if "todos" not in task_data: task_data["todos"] = []
        task_data["todos"].append({"title": content[:80], "priority": "medium", "status": "pending"})
        write_memory("task", task_data, merge=False)
        state["summary"] = f"📝 任务已记录 (task)"
    else:
        add_episodic(content, tags=[mtype])
        state["summary"] = f"📝 已记忆"

    save_trace("memory", {"decision": state["decision"], "type": mtype, "content": content[:100]})
    state["success"] = True
    return state


_graph = None


def build_memory_graph():
    global _graph
    if _graph: return _graph
    builder = StateGraph(MemoryAgentState)
    builder.add_node("decide", decide_node)
    builder.add_node("write", write_node)
    builder.add_edge("__start__", "decide")
    builder.add_conditional_edges("decide",
        lambda s: "write" if s.get("decision") in ("write", "update") else "__end__",
        {"write": "write", "__end__": "__end__"})
    builder.add_edge("write", "__end__")
    _graph = builder.compile(checkpointer=MemorySaver())
    return _graph


def run_memory_agent(trigger_text: str, user_id: str = "default_user",
                     memory_type: str | None = None) -> dict[str, Any]:
    start = time.monotonic()
    graph = build_memory_graph()
    initial = {
        "user_id": user_id, "trigger_text": trigger_text,
        "decision": "skip", "decision_reason": "", "target_type": "episodic",
        "content": "", "summary": "", "success": True, "error": None,
    }
    try:
        result = graph.invoke(initial, {"configurable": {"thread_id": f"mem_{uuid.uuid4().hex[:10]}"}})
        latency = int((time.monotonic() - start) * 1000)
        return {"success": True, "decision": result.get("decision"),
                "summary": result.get("summary", ""), "latency_ms": latency}
    except Exception as exc:
        logger.error("memory_failed", error=str(exc))
        return {"success": False, "error": str(exc)}
