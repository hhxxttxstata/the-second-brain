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
    is_profile_update,
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

## Memory types:
- "episodic" — an event, experience, or fact
- "task" — a todo / task to do
- "profile" — a personal attribute or preference (e.g. habit, phone number, taste)
- "semantic_knowledge" — a distilled conclusion / reusable standard / decision outcome
  (use when the user asks to SUMMARIZE a discussion or CONCLUSION and record it as reference knowledge:
   "总结一下...记下来", "提炼结论", "作为知识库/标准/规范", "以后新项目参考")

## Output — JSON:
{{"decision":"write|skip","reason":"why","type":"episodic|task|profile|semantic_knowledge","content_memory":"the key info to remember in 1-2 sentences (for semantic_knowledge: include the distilled standard/conclusion, NOT 'user asked me to summarize')"}}
"""


def decide_node(state: MemoryAgentState) -> MemoryAgentState:
    logger.info("memory.decide", step="🧠 判断是否值得记忆...")
    ctx = build_context(task=state.get("trigger_text", ""), max_tokens=1000,
                         session_id="memory_graph")
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
        if state["decision"] in ("write", "update"):
            logger.info("memory.decide.yes",
                        step=f"✅ 决定记忆 (type={state['target_type']})",
                        reason=state["decision_reason"])
        else:
            logger.info("memory.decide.skip",
                        step="⏭️ 跳过记忆",
                        reason=state["decision_reason"])
    except Exception:
        state["decision"] = "skip"
        logger.warning("memory.decide.error", step="⚠️ 解析失败，默认跳过")
    return state


def write_node(state: MemoryAgentState) -> MemoryAgentState:
    if state.get("decision") not in ("write", "update"):
        reason = state.get("decision_reason", "")
        if reason:
            # skip 时给出说明性输出（如'目标已存在'），而非空串
            summary = f"⏭️ 无需重复记忆: {reason[:120]}"
        else:
            summary = "(skipped)"
        logger.info("memory.write.skip", step=f"⏭️ 无需写入: {summary[:60]}")
        return {**state, "summary": summary, "success": True}

    mtype = state.get("target_type", "episodic")
    content = state.get("content", state.get("trigger_text", ""))[:500]
    logger.info("memory.write", step=f"💾 保存到 {mtype} 记忆...")

    if mtype == "profile":
        # 写入 stable_profile（缺陷二修复）
        is_prof, field, value = is_profile_update(state.get("trigger_text", ""))
        if is_prof and field not in ("", "_llm_resolve"):
            write_memory("stable_profile", {field: value}, merge=True)
            state["summary"] = f"📝 用户画像已更新: {field}={value}"
        elif is_prof and field == "_llm_resolve":
            # 泛指指令：用 LLM 提取的 content_memory 写入 profile
            write_memory("stable_profile", {"_指令": content[:80]}, merge=True)
            state["summary"] = f"📝 用户画像已更新: {content[:60]}..."
        else:
            add_episodic(content, tags=["auto"])
            state["summary"] = f"📝 Agent 记忆已保存 (episodic)"
    elif mtype == "semantic_knowledge":
        # 语义知识：写入 topic memory + episodic（结构化沉淀）
        add_episodic(content, tags=["semantic_knowledge", "auto"])
        try:
            from ..topic_memory import write_topic
            write_topic("projects", f"## 技术选型标准\n{content}\n", append=True)
        except Exception:
            pass
        state["summary"] = f"📚 语义知识已沉淀: {content[:80]}"
    elif mtype == "episodic":
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
    logger.info("memory.write.done", step=f"✅ 记忆保存完成: {state['summary']}")
    return state


_graph = None


def build_memory_graph():
    global _graph
    if _graph: return _graph
    builder = StateGraph(MemoryAgentState)
    builder.add_node("decide", decide_node)
    builder.add_node("write", write_node)
    builder.add_edge("__start__", "decide")
    # skip 也走 write_node（生成说明性 summary），避免返回空输出
    builder.add_edge("decide", "write")
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
