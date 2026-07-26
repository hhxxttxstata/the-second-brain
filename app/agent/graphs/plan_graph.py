"""Plan Agent — Obsidian vault 只读 + agent_data 读写。

流程: gather (读 vault 日记 + agent_data 记忆) → plan (LLM) → reflect (LLM) ──→ commit (写 agent_data)
                                                                     ↑            │
                                                                     └── replan ──┘
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.core.logging import logger
from ..agent_data_service import (
    add_episodic,
    build_context,
    format_profile,
    format_tasks,
    read_memory,
    read_today_state,
    save_today_state,
    save_trace,
    write_memory,
)
from app.obsidian import vault
from .llm import get_chat_model


class PlanState(TypedDict):
    user_id: str
    plan_date: str
    run_id: str

    agent_context: str       # 来自 agent_data 的记忆
    today_diary: str         # 来自 vault 的今日日记
    recent_diaries: str      # 来自 vault 的近期日记
    profile_text: str        # agent_data profile

    plan_raw: str
    plan_items: list[dict]
    plan_id: str | None

    reflect_attempts: int
    reflect_feedback: str

    latency_ms: int
    success: bool
    error: str | None


def gather_node(state: PlanState) -> PlanState:
    """从 vault 读日记 + agent_data 读记忆，不写 vault。"""
    state["run_id"] = f"plan_{uuid.uuid4().hex[:10]}"
    today = state.get("plan_date", date.today().isoformat())

    logger.info("plan.gather", step="📖 读取用户画像和记忆...")
    # agent_data 记忆
    state["profile_text"] = format_profile()
    state["agent_context"] = build_context(task="daily plan", max_tokens=2000)["context"]

    # vault 日记（只读）
    logger.info("plan.gather", step=f"📖 读取今日日记 ({today}.md)...")
    diary_path = f"diaries/{today}.md"
    diary = vault.read_file(diary_path, max_chars=8000)
    state["today_diary"] = diary if "不存在" not in diary else "(今天还没有日记)"

    logger.info("plan.gather", step="📖 读取近期日记...")
    diaries = vault.read_folder("diaries", max_files=5, max_chars_per_file=3000)
    state["recent_diaries"] = diaries if "不存在" not in diaries else "(none)"

    logger.info("plan.gather.done", step="✅ 资料收集完成")
    return state


PLAN_PROMPT = """You are a daily planning assistant. Generate a focused plan from the user's diary and memory context.

## Output format — JSON list:
[
  {{"title": "string", "description": "string", "priority": "high|medium|low", "source": "diary|memory|goal|new"}}
]

## User profile:
{profile}

## Today's diary:
{diary}

## Recent diaries:
{recent}

## Agent memory context:
{context}
"""


def plan_node(state: PlanState) -> PlanState:
    logger.info("plan.planning", step=f"🤖 LLM 生成计划（基于 {len(state.get('plan_items', []))} 条已有数据）...")
    prompt = PLAN_PROMPT.format(
        profile=state.get("profile_text", "{}"),
        diary=state.get("today_diary", ""),
        recent=state.get("recent_diaries", ""),
        context=state.get("agent_context", ""),
    )
    model = get_chat_model(temperature=0.5)
    response = model.invoke(prompt)
    state["plan_raw"] = response.content if hasattr(response, "content") else str(response)
    import re
    try:
        cleaned = state["plan_raw"].strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned).rstrip("` \n")
        state["plan_items"] = json.loads(cleaned)
        if not isinstance(state["plan_items"], list):
            state["plan_items"] = [state["plan_items"]]
        logger.info("plan.planning.done", step=f"✅ 计划生成完毕，共 {len(state['plan_items'])} 项")
    except Exception:
        logger.warning("plan_parse_failed", raw=state["plan_raw"][:200])
        state["plan_items"] = [{"title": "Daily review", "priority": "medium",
                                "description": "Review today", "source": "default"}]
    return state


REFLECT_PROMPT = """Evaluate this plan:

{plan}

Context — profile: {profile}, diary: {diary}

One line: "ok" or what's wrong."""


def reflect_node(state: PlanState) -> PlanState:
    state["reflect_attempts"] = state.get("reflect_attempts", 0) + 1
    logger.info("plan.reflect", step=f"🔍 反思计划（第 {state['reflect_attempts']} 次）...")
    plan_text = json.dumps(state.get("plan_items", []), ensure_ascii=False, indent=2)
    prompt = REFLECT_PROMPT.format(
        plan=plan_text, profile=state.get("profile_text", "")[:300],
        diary=state.get("today_diary", "")[:300],
    )
    model = get_chat_model(temperature=0.3)
    response = model.invoke(prompt)
    feedback = (response.content if hasattr(response, "content") else str(response)).strip().lower()
    state["reflect_feedback"] = "ok" if feedback.startswith("ok") else feedback
    if state["reflect_feedback"] == "ok":
        logger.info("plan.reflect.pass", step="✅ 计划通过反思")
    else:
        logger.info("plan.reflect.retry", step=f"🔄 计划需调整: {state['reflect_feedback'][:60]}")
    return state


def should_replan(state: PlanState) -> Literal["commit", "replan"]:
    if state["reflect_attempts"] >= 2: return "commit"
    if state["reflect_feedback"] != "ok": return "replan"
    return "commit"


def commit_node(state: PlanState) -> dict:
    """保存计划到 agent_data（不写 vault）。"""
    today = state.get("plan_date", date.today().isoformat())
    items = state.get("plan_items", [])
    plan_id = f"plan_{uuid.uuid4().hex[:8]}"

    logger.info("plan.commit", step=f"💾 保存计划结果共 {len(items)} 项...")

    # 写入 agent_data/memory/task_memory.json
    task_data = read_memory("task")
    if "history" not in task_data:
        task_data["history"] = []
    task_data["history"].append({
        "plan_id": plan_id, "date": today,
        "summary": f"{len(items)} items",
        "items": items,
    })
    task_data["todos"] = [
        {"title": it.get("title", ""), "priority": it.get("priority", "medium"),
         "status": "pending"} for it in items
    ]
    write_memory("task", task_data, merge=False)

    # 写入 agent_data/state/today.json
    save_today_state({"plan_id": plan_id, "plan_items_count": len(items)})

    # 写 trace
    save_trace("daily_plan", {
        "plan_id": plan_id, "items": items,
        "reflect_attempts": state["reflect_attempts"],
    })

    # 加一条情景记忆
    add_episodic(f"生成了今日计划 ({today})，共 {len(items)} 项",
                 tags=["plan"])

    state["plan_id"] = plan_id
    logger.info("plan.commit.done", step="✅ 计划已存储，执行完毕")
    return state


_graph = None


def build_plan_graph():
    global _graph
    if _graph: return _graph
    builder = StateGraph(PlanState)
    builder.add_node("gather", gather_node)
    builder.add_node("plan", plan_node)
    builder.add_node("reflect", reflect_node)
    builder.add_node("commit", commit_node)
    builder.add_edge("__start__", "gather")
    builder.add_edge("gather", "plan")
    builder.add_edge("plan", "reflect")
    builder.add_conditional_edges("reflect", should_replan, {"commit": "commit", "replan": "plan"})
    builder.add_edge("commit", "__end__")
    _graph = builder.compile(checkpointer=MemorySaver())
    return _graph


def run_plan_graph(user_id: str = "default_user",
                   plan_date: str | None = None) -> dict[str, Any]:
    start = time.monotonic()
    plan_date = plan_date or date.today().isoformat()
    graph = build_plan_graph()
    initial = {
        "user_id": user_id, "plan_date": plan_date, "run_id": f"plan_{uuid.uuid4().hex[:10]}",
        "agent_context": "", "today_diary": "", "recent_diaries": "", "profile_text": "",
        "plan_raw": "", "plan_items": [], "plan_id": None,
        "reflect_attempts": 0, "reflect_feedback": "", "latency_ms": 0, "success": True, "error": None,
    }
    try:
        result = graph.invoke(initial, {"configurable": {"thread_id": initial["run_id"]}})
        latency = int((time.monotonic() - start) * 1000)
        return {"success": True, "plan_id": result.get("plan_id"), "date": plan_date,
                "items": result.get("plan_items", []), "latency_ms": latency,
                "reflect_attempts": result.get("reflect_attempts", 1)}
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        logger.error("plan_failed", error=str(exc))
        return {"success": False, "error": str(exc), "latency_ms": latency}
