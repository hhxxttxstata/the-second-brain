"""Memory Agent — 写 agent_data/memory/ JSON 文件，不写 vault。"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

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
    privacy_requested: bool


# 显式记忆指令：用户明确要求存储时，LLM 误判 skip 也要尊重用户意图
_MEMORY_CMD_HINTS = (
    "存入记忆", "写入记忆", "存进记忆", "记住", "记一下", "记下来",
    "帮我记", "请你记", "别忘了", "别忘", "保存这个",
    "save this", "remember", "write memory",
)

# 隐私隔离诉求：需要落实/确认 git ignore 隔离（不得以"无需"搪塞）
_PRIVACY_HINTS = (
    "git ignore", "gitignore", "git 忽略",
    "隐私", "隔离", "不进 git", "不进git",
    "不要提交", "别提交", "不提交", "保密", "安全存储",
)

# 记忆存放目录 agent_data（相对项目根）— 隐私隔离的检查/配置目标
# memory_graph.py 位于 app/agent/graphs/ → 上溯 4 层到项目根
_GITIGNORE_PATH = Path(__file__).resolve().parent.parent.parent.parent / ".gitignore"


def _ensure_gitignore_isolation() -> tuple[bool, str]:
    """确保记忆存放目录 agent_data 被 .gitignore 忽略（幂等）。

    Returns:
        (ok, status): status ∈ {"exists"（已隔离）, "added"（本次追加）, "error"}
    """
    try:
        gi = _GITIGNORE_PATH
        content = gi.read_text(encoding="utf-8") if gi.exists() else ""
        lines = [ln.strip() for ln in content.splitlines()]

        import re
        covered = any(re.fullmatch(r"agent_data/?\*?", ln) for ln in lines)
        if covered:
            return True, "exists"

        # 未隔离 → 幂等追加（避免重复写入）
        if not any(ln.startswith("agent_data") for ln in lines):
            header = (
                "\n# Agent data (runtime) — 包含个人记忆，不提交（由 Agent 隐私隔离添加）\n"
                "agent_data/*\n"
                "!agent_data/eval/\n"
            )
            with gi.open("a", encoding="utf-8") as f:
                if content and not content.endswith("\n"):
                    f.write("\n")
                f.write(header)
        return True, "added"
    except Exception:
        return False, "error"


def _append_privacy_summary(summary: str) -> str:
    """在 summary 上附加隐私隔离说明（已确认/已配置/失败）。"""
    ok, status = _ensure_gitignore_isolation()
    if ok and status == "exists":
        return f"{summary}\n🔒 隐私隔离已确认：agent_data 已在 .gitignore 中（记忆默认不进 git）"
    if ok:
        return f"{summary}\n🔒 隐私隔离已配置：已将 agent_data 加入 .gitignore"
    return f"{summary}\n⚠️ 隐私隔离未能自动配置（请手动检查 .gitignore）"


DECIDE_PROMPT = """Evaluate this input for memory-worthiness.

## Rules:
- If the user EXPLICITLY asks to remember/store something ("存入记忆", "记住", "记一下", "写入记忆", "帮我记", "别忘了") → decide "write": the user explicitly asked to store, do not skip.
- If the user mentions privacy isolation ("git ignore", "gitignore", "隐私", "隔离", "不要提交") → it is a legitimate request. Memory data lives in agent_data/, which is git-ignored by default; the system will verify/configure isolation. Do NOT claim "git ignore is unnecessary".

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
    trigger = state.get("trigger_text", "")
    trigger_l = trigger.lower()
    explicit_cmd = any(h in trigger_l for h in _MEMORY_CMD_HINTS)
    privacy = any(h in trigger_l for h in _PRIVACY_HINTS)
    state["privacy_requested"] = privacy

    ctx = build_context(task=trigger, max_tokens=1000,
                         session_id="memory_graph")
    prompt = DECIDE_PROMPT.format(
        trigger_text=trigger,
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
        state["content"] = data.get("content_memory", trigger)
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

    # 显式记忆指令 → 尊重用户意图（LLM 误判 skip 时强制 write；
    # skip 响应的 content 不可靠，此时用确定性内容）
    if explicit_cmd and state.get("decision") == "skip":
        state["decision"] = "write"
        if privacy:
            state["target_type"] = "profile"
            state["content"] = "用户偏好：隐私内容需 git ignore 隔离，记忆数据（agent_data）不进 git"
        else:
            state["target_type"] = state.get("target_type") or "episodic"
            state["content"] = trigger[:200]
        logger.info("memory.decide.forced",
                    step="✅ 显式指令强制写入",
                    reason="用户明确要求记忆（LLM 误判 skip）")
    return state


def write_node(state: MemoryAgentState) -> MemoryAgentState:
    _t0 = time.monotonic()  # 蒸馏窗口对比依赖 trace 的 latency 指标
    privacy = bool(state.get("privacy_requested"))
    if state.get("decision") not in ("write", "update"):
        reason = state.get("decision_reason", "")
        if reason:
            # skip 时给出说明性输出（如'目标已存在'），而非空串
            summary = f"⏭️ 无需重复记忆: {reason[:120]}"
        else:
            summary = "(skipped)"
        # 隐私隔离诉求：即使本次不写记忆，隔离也应落实/确认
        if privacy:
            summary = _append_privacy_summary(summary)
        logger.info("memory.write.skip", step=f"⏭️ 无需写入: {summary[:60]}")
        return {**state, "summary": summary, "success": True}

    mtype = state.get("target_type", "episodic")
    content = state.get("content", state.get("trigger_text", ""))[:500]
    logger.info("memory.write", step=f"💾 保存到 {mtype} 记忆...")

    if mtype == "profile":
        # 写入 stable_profile（条目化存储：保留字段变更历史与来源）
        is_prof, field, value = is_profile_update(state.get("trigger_text", ""))
        if is_prof and field not in ("", "_llm_resolve"):
            res = write_memory("stable_profile", {field: value}, merge=True, source="memory_graph")
            changes = res.get("changes", [])
            if changes:
                state["summary"] = "📝 画像已更新: " + "; ".join(changes)
            else:
                state["summary"] = f"📝 用户画像已确认: {field}={value}"
        elif is_prof and field == "_llm_resolve":
            # 泛指指令：用 LLM 提取的 content_memory 写入 profile
            res = write_memory("stable_profile", {"_指令": content[:80]}, merge=True, source="memory_graph")
            changes = res.get("changes", [])
            if changes:
                state["summary"] = "📝 画像已更新: " + "; ".join(changes)
            else:
                state["summary"] = f"📝 用户画像已确认: {content[:60]}..."
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

    save_trace("memory", {"decision": state["decision"], "type": mtype, "content": content[:100],
                          "latency_ms": int((time.monotonic() - _t0) * 1000)})
    state["success"] = True
    # 隐私隔离：用户要求 git ignore/隐私隔离时，落实或确认 .gitignore
    if privacy:
        state["summary"] = _append_privacy_summary(state["summary"])
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
    _graph = builder.compile()
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
        result = graph.invoke(initial)
        latency = int((time.monotonic() - start) * 1000)
        return {"success": True, "decision": result.get("decision"),
                "summary": result.get("summary", ""), "latency_ms": latency}
    except Exception as exc:
        logger.error("memory_failed", error=str(exc))
        return {"success": False, "error": str(exc)}
