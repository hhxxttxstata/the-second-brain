"""Reflection Agent — 使用 Context Builder 合并两层后分析。

vault 只读（知识资产），agent_data 读写（记忆）。
"""
from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.core.logging import logger
from ..agent_data_service import add_episodic, build_context, save_trace
from app.obsidian import vault
from .llm import get_chat_model


class ReflectState(TypedDict):
    subject: str
    content: str
    context: str
    analysis: str
    critique: str
    suggestions: str
    summary: str
    success: bool
    error: str | None


ANALYZE_PROMPT = """Analyze this content.

## Subject: {subject}
## Content:
{content}

## User context (profile + memory + vault):
{context}

3-5 sentences of analysis."""

CRITIQUE_PROMPT = """Constructive critique of:

## Content:
{content}
## Prior analysis:
{analysis}

2-4 sentences."""

SUGGEST_PROMPT = """Suggestions based on:

## Subject: {subject}
## Analysis: {analysis}
## Critique: {critique}
## Content: {content}

## Output — JSON:
{{"summary": "...", "key_insights": ["..."], "action_items": ["..."]}}
"""


def gather_context(state: ReflectState) -> ReflectState:
    """合并两层上下文。"""
    ctx = build_context(task=state.get("content", "")[:100], max_tokens=2000)
    state["context"] = ctx["context"]
    return state


def analyze_node(state: ReflectState) -> ReflectState:
    model = get_chat_model(temperature=0.5)
    prompt = ANALYZE_PROMPT.format(
        subject=state.get("subject", "general"), content=state.get("content", ""),
        context=state.get("context", ""),
    )
    response = model.invoke(prompt)
    state["analysis"] = response.content if hasattr(response, "content") else str(response)
    return state


def critique_node(state: ReflectState) -> ReflectState:
    model = get_chat_model(temperature=0.6)
    prompt = CRITIQUE_PROMPT.format(content=state.get("content", ""), analysis=state.get("analysis", ""))
    response = model.invoke(prompt)
    state["critique"] = response.content if hasattr(response, "content") else str(response)
    return state


def suggest_node(state: ReflectState) -> ReflectState:
    model = get_chat_model(temperature=0.4)
    prompt = SUGGEST_PROMPT.format(
        subject=state.get("subject", "general"), content=state.get("content", ""),
        analysis=state.get("analysis", ""), critique=state.get("critique", ""),
    )
    try:
        response = model.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        import re
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned).rstrip("` \n")
        data = json.loads(cleaned)
        state["summary"] = data.get("summary", text[:500])
        state["suggestions"] = "\n".join(f"- {a}" for a in data.get("action_items", []))
    except Exception:
        state["summary"] = text[:500]
        state["suggestions"] = "(parse error)"
    state["success"] = True

    # 写 trace + 记忆
    save_trace("reflect", {"subject": state.get("subject"), "summary": state.get("summary", "")[:200]})
    add_episodic(f"反思: {state.get('subject')} — {state.get('summary', '')[:100]}", tags=["reflect"])

    return state


_graph = None


def build_reflect_graph():
    global _graph
    if _graph: return _graph
    builder = StateGraph(ReflectState)
    builder.add_node("gather_context", gather_context)
    builder.add_node("analyze", analyze_node)
    builder.add_node("critique", critique_node)
    builder.add_node("suggest", suggest_node)
    builder.add_edge("__start__", "gather_context")
    builder.add_edge("gather_context", "analyze")
    builder.add_edge("analyze", "critique")
    builder.add_edge("critique", "suggest")
    builder.add_edge("suggest", "__end__")
    _graph = builder.compile(checkpointer=MemorySaver())
    return _graph


def run_reflect(subject: str, content: str, user_id: str = "default_user") -> dict[str, Any]:
    import time, uuid
    start = time.monotonic()
    graph = build_reflect_graph()
    initial = {
        "subject": subject, "content": content, "context": "",
        "analysis": "", "critique": "", "suggestions": "", "summary": "",
        "success": True, "error": None,
    }
    try:
        result = graph.invoke(initial, {"configurable": {"thread_id": f"reflect_{uuid.uuid4().hex[:10]}"}})
        latency = int((time.monotonic() - start) * 1000)
        return {"success": True, "analysis": result.get("analysis", ""),
                "critique": result.get("critique", ""), "suggestions": result.get("suggestions", ""),
                "summary": result.get("summary", ""), "latency_ms": latency}
    except Exception as exc:
        logger.error("reflect_failed", error=str(exc))
        return {"success": False, "error": str(exc)}
