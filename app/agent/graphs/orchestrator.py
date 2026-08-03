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
    run_id: str | None       # 运行 ID

    step_log: list[dict]     # 步骤日志，供 UI 展示思考过程


# ---------------------------------------------------------------------------
# Supervisor prompt
# ---------------------------------------------------------------------------

SUPERVISOR_PROMPT = """You are the orchestrator of a personal knowledge agent system. Analyze the user's
input and route it to the correct sub-agent.

## Available agents:
1. **chatbot** — general conversation, Q&A, casual chat (default). Handles: questions, searches, diary queries, book recommendations, fund queries — anything that needs to read vault notes or use tools.
2. **plan** — daily plan generation + task/todo management (add/delete/update/merge/split todos). "generate daily plan", "what should I do today", "今日计划"
3. **reflect** — "reflect on this", "analyze", "critique", "帮我分析", "反思"
4. **memory** — "remember", "don't forget", "save this", "write memory", "记忆"

## Routing rules:
- If the user is **questioning or disputing** existing plan items or notes ("我哪里说了要去", "被污染", "我没说过"), rather than just operating on tasks → route to **chatbot** (it reads vault + memory to verify the source first)
- If the user asks about or operates on **tasks/todos/待办** (add, delete, merge, split, update, mark, set status) and IS NOT disputing the source → route to **plan**
- If the user asks "what did I write", "search my notes", "最近写了什么", "帮我搜", or wants to search diary/notes → route to **chatbot** (it reads the vault)
- If the user says "analyze", "reflect", "反思" → route to **reflect**
- If the user says "plan", "今日计划", "daily plan" → route to **plan**
- If the user asks "please write", "save this", "remember", "记忆" → route to **memory**
- **If the user asks about MEDICAL topics** (肺栓塞, CTPA, 深静脉血栓, 抗凝, 医学文献, PE, D-dimer, 肺动脉) → route to **chatbot** (it has medical_rag_query / medical_pe_diagnosis tools to answer from the medical knowledge base)
- **If the user is UPDATING or CORRECTING a previously-saved memory** ("我之前说...现在改了", "以前是...现在改成", "改成", "以后都", "更正", "更新一下"), and the input is primarily about personal facts/habits/preferences → route to **memory** (it detects conflicts and supersedes old memories)
- If the user asks questions about themselves ("我叫什么", "我的背景", "心情怎么样", personal info queries) → route to **chatbot** (it can read memory + vault to answer)
- **If the input is primarily a conversation / question / status update / complaint, but ALSO contains a small memory instruction (e.g. "对了...你记一下", "以后...都", "我换了...")** → route to **chatbot** (it can write memory from within conversation). Memory instructions embedded in conversation do NOT require routing to memory. Only route to memory when the ENTIRE input is a memory-save request.
- For anything else (conversation, Q&A, recommendations, queries) → route to **chatbot**

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
    # conversation 可能是 list[str]（旧格式）或 list[dict]（新格式）
    raw_conv = state.get("conversation", []) or []
    if raw_conv and isinstance(raw_conv[0], dict):
        conv_text = "\n".join(
            f"{t.get('role', '?')}: {t.get('content', '')[:200]}"
            for t in raw_conv[-5:]
        ) or "(none)"
    else:
        conv_text = "\n".join(str(t)[:200] for t in raw_conv[-5:]) or "(none)"

    logger.info("orchestrator.supervisor", msg="分析用户意图，决定路由...")
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
        logger.info("orchestrator.route",
                    route=state["route"],
                    reason=state["route_reason"])
    except Exception:
        state["route"] = "chatbot"
        state["route_reason"] = "fallback: parse error"
        logger.warning("orchestrator.route_fallback", error="parse error")
    return state


def execute_agent(state: OrchestratorState) -> OrchestratorState:
    """执行选中的子 Agent，带 Trace。"""
    route = state.get("route", "chatbot")
    user_id = state.get("user_id", "default_user")
    text = state.get("input_text", "")

    route_labels = {
        "chatbot": "💬 对话回答",
        "plan": "📋 生成计划",
        "reflect": "🔍 反思分析",
        "memory": "🧠 记忆处理",
    }
    logger.info("orchestrator.execute",
                route=route,
                step=f"开始执行 → {route_labels.get(route, route)}")

    # 创建 Trace
    trace = TraceRecord(route, text[:100])
    state["run_id"] = trace.trace_id
    from ..trace import set_current_trace
    set_current_trace(trace)

    try:
        trace.start()

        if route == "plan":
            # 判断是 task 操作还是每日计划
            # 注意：有些词（todo/task/计划）在"读取参考"和"执行操作"时都会出现
            # 需要区分：包含"生成计划/写计划/明天的计划" = 计划生成，不归 task_ops
            text_lower = text.lower()

            # 计划生成关键词（走 plan_graph 而非 task_ops）
            plan_keywords = ["生成.*计划", "写.*计划", "明天的计划", "今日计划",
                             "daily plan", "今天的计划"]
            is_plan_request = False
            import re
            for pk in plan_keywords:
                if re.search(pk, text_lower):
                    is_plan_request = True
                    break

            # 任务操作关键词（走 task_ops）
            task_action_kw = ["添加", "删除", "删掉", "新建", "改成",
                              "拆成", "拆分", "merge", "合并", "标记",
                              "状态", "强制", "截止日期", "拆分"]
            is_task_op = any(kw in text_lower for kw in task_action_kw)

            if is_plan_request and not is_task_op:
                # 明确是生成计划，关键词'Todo'只是参考数据
                logger.info("plan.run", step="📋 读取日记和记忆并生成计划...")
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
                    logger.info("plan.complete", step=f"✅ 计划生成完成，共 {len(items)} 项")
                else:
                    state["result"] = f"❌ 生成计划失败: {r.get('error', '')}"
                    logger.error("plan.failed", error=r.get('error', ''))
            elif is_task_op:
                logger.info("plan.task_ops", step="📋 执行任务操作...")
                from .plan_graph import run_task_ops
                r = run_task_ops(user_input=text, user_id=user_id)
                state["result_data"] = r
                if r.get("success"):
                    changes = r.get("changes", [])
                    summary = r.get("summary", "")
                    if changes:
                        state["result"] = f"✅ 任务操作成功:\n" + "\n".join(f"  · {c}" for c in changes)
                    else:
                        state["result"] = f"ℹ️ {summary}"
                    logger.info("plan.task_ops.done", step=f"✅ 任务操作完成: {summary[:80]}")
                else:
                    state["result"] = f"❌ 任务操作失败: {r.get('error', '')}"
            else:
                logger.info("plan.run", step="📋 读取日记和记忆并生成计划...")
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
                    logger.info("plan.complete", step=f"✅ 计划生成完成，共 {len(items)} 项")
                else:
                    state["result"] = f"❌ 生成计划失败: {r.get('error', '')}"
                    logger.error("plan.failed", error=r.get('error', ''))

        elif route == "reflect":
            logger.info("reflect.run", step=f"🔍 开始反思分析: {text[:60]}...")
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
                logger.info("reflect.complete", step="✅ 反思分析完成")
            else:
                state["result"] = f"❌ 反思失败: {r.get('error', '')}"
                logger.error("reflect.failed", error=r.get('error', ''))

        elif route == "memory":
            logger.info("memory.run", step=f"🧠 判断是否需要记忆: {text[:60]}...")
            r = run_memory_agent(trigger_text=text, user_id=user_id)
            state["result_data"] = r
            state["result"] = r.get("summary", "记忆处理完成")
            if r.get("decision") in ("write", "update"):
                trace.add_memory_update("episodic", state["result"][:100])
                logger.info("memory.saved", step=f"✅ 已保存记忆: {state['result']}")
            else:
                logger.info("memory.skip", step="⏭️ 无需记忆，已跳过")

        else:  # chatbot
            logger.info("chatbot.run", step=f"💬 构建上下文并回答: {text[:60]}...")
            from .chatbot_graph import build_chatbot_graph
            from langchain_core.messages import HumanMessage, AIMessage
            from ..agent_data_service import build_context

            # 先构建上下文并记录到 trace
            ctx = build_context(task=text, session_id=state.get("run_id", ""))
            for s in ctx.get("sources", []):
                trace.add_context_source(
                    s.get("layer", "?"), s.get("type", "?"),
                    len(ctx.get("context", "")),
                )

            graph = build_chatbot_graph()

            # 构造消息列表：注入历史对话（来自持久化会话）+ 当前输入
            messages: list = []
            conv = state.get("conversation", [])
            if conv:
                # ── Context Pressure Monitor: 历史超预算时自动压缩 ──
                try:
                    from ..context_pressure import (
                        measure_pressure, compress_history, format_pressure_line,
                    )
                    press = measure_pressure(history=conv)
                    if press["level"] in ("yellow", "red"):
                        compressed = compress_history(
                            conv, session_id=state.get("run_id", ""))
                        logger.info("context.pressure",
                                    step=format_pressure_line(press),
                                    compressed=len(compressed) < len(conv),
                                    turns=f"{len(conv)}→{len(compressed)}")
                        conv = compressed
                        press["compressed"] = len(compressed) < len(conv)
                        # 记录到 trace
                        try:
                            trace.context_sources.append({
                                "layer": "observability",
                                "type": "context_pressure",
                                "chars": 0,
                                "preview": format_pressure_line(press),
                            })
                        except Exception:
                            pass
                except Exception:
                    pass

                for turn in conv:
                    if isinstance(turn, dict):
                        role = turn.get("role", "")
                        content = turn.get("content", str(turn))
                    else:
                        # conversation 传进来是旧 dict 格式
                        # format: {"role": "human"|"ai", "content": "..."}
                        # 但旧代码可能传的是 list[str]
                        try:
                            content = str(turn)
                            role = "human"
                        except Exception:
                            continue

                    if role == "human" or role == "user":
                        messages.append(HumanMessage(content=content))
                    elif role == "ai" or role == "assistant":
                        # 历史 AI 回答太长时截断后半段（保留开头包含分析/方案的部分）
                        if len(content) > 1200:
                            # 保留前 800 字（含方案）+ 最后 400 字（含结论）
                            content = content[:800] + "\n\n...(省略中间)...\n\n" + content[-400:]
                        messages.append(AIMessage(content=content))
                    else:
                        messages.append(HumanMessage(content=content))

            messages.append(HumanMessage(content=text))

            # 用 run_id 做 thread_id，保证 LangGraph MemorySaver 同一会话
            chat_thread = f"orch_chat_{state.get('run_id', uuid.uuid4().hex[:10])}"
            config = {"configurable": {"thread_id": chat_thread}}
            response = graph.invoke(
                {"messages": messages},
                config,
            )
            last_msg = response["messages"][-1]
            state["result"] = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
            state["result_data"] = {"message_count": len(response["messages"])}
            logger.info("chatbot.complete", step="✅ 回答完成")

        state["success"] = True
        trace.final_output = state.get("result", "")[:500]
        trace.success = True

    except Exception as exc:
        error_msg = str(exc)
        # 截断超长 API 错误消息，保留关键信息
        if "Error code:" in error_msg and len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."
        logger.error("orchestrator_exec_failed", route=route, error=error_msg)
        state["result"] = f"❌ 执行 {route} 时出错: {error_msg}"
        state["error"] = error_msg
        state["success"] = False
        trace.success = False
        trace.error = error_msg

    finally:
        set_current_trace(None)
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
    logger.info("orchestrator.start", step="🚀 Orchestrator 启动", text=input_text[:80])

    graph = build_orchestrator()
    run_id = thread_id or f"orch_{uuid.uuid4().hex[:10]}"
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
        "run_id": run_id,
        "step_log": [],
    }

    try:
        result = graph.invoke(initial, {"configurable": {"thread_id": run_id}})
        latency = int((time.monotonic() - start) * 1000)
        route = result.get("route", "?")
        success = result.get("success", False)
        logger.info("orchestrator.done",
                    step=f"✅ 执行完毕 (route={route})",
                    latency=f"{latency}ms",
                    success=success)
        return {
            "success": result.get("success", False),
            "route": result.get("route", "?"),
            "route_reason": result.get("route_reason", ""),
            "result": result.get("result", ""),
            "result_data": result.get("result_data", {}),
            "latency_ms": latency,
            "run_id": result.get("run_id", ""),
        }
    except Exception as exc:
        logger.error("orchestrator.failed", step="❌ Orchestrator 执行失败", error=str(exc))
        return {"success": False, "error": str(exc), "route": "error", "result": str(exc)}
