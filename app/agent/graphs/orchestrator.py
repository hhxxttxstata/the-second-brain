"""Orchestrator — 多 Agent 编排。Planner + Task Graph 范式（2026-08 改造）。

个人知识助手天然是多目标：一次对话可能同时产生回答、提取知识、更新记忆、
反思、规划下一步。因此本模块不是单路由（routing problem），而是任务编排
（orchestration problem）：

    User
     ↓
  Planner（LLM 分析意图 → 生成有序子任务列表）
     ↓
  Task Graph（按序执行每个子任务，各自路由到子 Agent）
  ┌──────────┬─────────────┬───────────┬──────────┐
  Memory     Reflection    Chat       Plan
  └──────────┴─────────────┴───────────┴──────────┘
     ↓
  State Merge（汇总所有子任务结果 → 最终回复）

设计约束：
- 单意图输入 → Planner 只产出 1 个任务 → 行为与旧单路由完全等价
  （route 字段 = 该任务 agent，grader 的"路由到 X"判定不受影响）
- 多意图输入 → 按依赖排序（先写类 memory/plan 操作，后读类 chatbot/reflect）
- 任一子任务失败不中断后续任务，最终 success = 全部成功
- LLM 输出兼容旧格式 {"route": ...} → 自动退化为单任务

支持:
- chatbot → 一般对话（默认）
- plan → 每日计划（vault 只读 + agent_data 读写）
- reflect → 反思分析（vault 只读 + agent_data 读写）
- memory → 记忆管理（写 agent_data/memory/）
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.core.logging import logger

from ..trace import TraceRecord, get_trace_stats
from .llm import get_chat_model
from .plan_graph import run_plan_graph
from .reflect_graph import run_reflect
from .memory_graph import run_memory_agent

# 并行执行上限（与 _MAX_TASKS 一致）
_MAX_PARALLEL = 4
# _llm_tokens 累计的并发保护（并行子任务同时写 state）
_USAGE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class OrchestratorState(TypedDict):
    user_id: str
    input_text: str          # 用户原始输入
    conversation: list       # 历史对话摘要

    route: str               # 主意图路由（= tasks[0].agent，兼容 grader "路由到 X"）
    route_reason: str        # planner 理由

    tasks: list              # Planner 产出的子任务列表 [{"agent","instruction"}, ...]
    task_results: list       # 每个子任务的执行结果

    result: str              # State Merge 后的最终回复
    result_data: dict        # 结构化结果

    success: bool
    error: str | None
    run_id: str | None       # 线程/会话 ID（调用方传入的 thread_id，跨调用稳定）
    trace_id: str | None     # 本次执行的 trace ID（evidence_refs 引用）
    _llm_tokens: dict        # 本轮 LLM usage 累计（planner + 子 agent 主路径）

    step_log: list[dict]     # 步骤日志，供 UI 展示思考过程


# ---------------------------------------------------------------------------
# Planner prompt
# ---------------------------------------------------------------------------

PLANNER_PROMPT = """You are the orchestrator of a personal knowledge agent system. Analyze the user's
input and route it to the correct sub-agent(s).

## Available agents:
1. **chatbot** — general conversation, Q&A, casual chat (default). Handles: questions, searches, diary queries, book recommendations, fund queries — anything that needs to read vault notes or use tools.
2. **plan** — daily plan generation + task/todo management (add/delete/update/merge/split todos). "generate daily plan", "what should I do today", "今日计划"
3. **reflect** — "reflect on this", "analyze", "critique", "帮我分析", "反思"
4. **memory** — "remember", "don't forget", "save this", "write memory", "记忆"

## Task decomposition rules:
- A personal knowledge assistant is MULTI-OBJECTIVE: one message may contain several independent intents
  (e.g. "记住我的目标 + 帮我生成今天的计划" = memory + plan). Decompose them into separate tasks.
- **Single intent → exactly ONE task.** Do NOT split one action into pieces, do NOT add tasks for
  trivial chat or for the user's tone/compliments.
- **Multiple independent intents → one task per intent.** Order matters:
  write-type tasks FIRST (memory saves, plan/todo operations), read-type tasks LAST (chatbot Q&A, reflect).
  If a later task depends on an earlier one's result, keep that order.
- Embedded small memory instructions inside a conversation ("对了...你记一下", "以后...都") do NOT
  become separate tasks — let chatbot handle them inline (chatbot can write memory from within conversation).
- Never output more than 4 tasks. When in doubt, prefer fewer tasks.
- Each task's "instruction" must be the SELF-CONTAINED text for that sub-agent — a faithful slice of the
  user's original words (or a minimal paraphrase), so the sub-agent can act on it alone.

## Parallelism (stage):
- Each task carries an integer "stage" (≥ 1). Tasks with the SAME stage run in PARALLEL;
  later stages start only after all earlier stages finish.
- **Independent tasks → same stage** (e.g. "记住偏好A" + "记住偏好B"; "生成今日计划" + "反思日记进展").
- **A task that READS the result of another must be in a LATER stage**
  (e.g. chatbot answering based on a memory just saved → chatbot stage 2, memory stage 1).
- chatbot tasks are always serialized with each other — never put two chatbot tasks in one stage.
- Single intent → one task, stage 1.

## Routing rules (single-task judgment):
- If the user is **questioning or disputing** existing plan items or notes ("我哪里说了要去", "被污染", "我没说过"), rather than just operating on tasks → route to **chatbot** (it reads vault + memory to verify the source first)
- If the user asks about or operates on **tasks/todos/待办** (add, delete, merge, split, update, mark, set status) and IS NOT disputing the source → route to **plan**
- If the user asks "what did I write", "search my notes", "最近写了什么", "帮我搜", or wants to search diary/notes → route to **chatbot** (it reads the vault)
- If the user says "analyze", "reflect", "反思" → route to **reflect**
- If the user says "plan", "今日计划", "daily plan" → route to **plan**
- If the user asks "please write", "save this", "remember", "记忆" → route to **memory**
- **If the user is UPDATING or CORRECTING a previously-saved memory** ("我之前说...现在改了", "以前是...现在改成", "改成", "以后都", "更正", "更新一下"), and the input is primarily about personal facts/habits/preferences → route to **memory** (it detects conflicts and supersedes old memories)
- If the user asks questions about themselves ("我叫什么", "我的背景", "心情怎么样", personal info queries) → route to **chatbot** (it can read memory + vault to answer)
- For anything else (conversation, Q&A, recommendations, queries) → route to **chatbot**

## User input:
{input}

## Conversation history:
{conversation}

## Output — JSON only:
{{
  "tasks": [
    {{"agent": "chatbot|plan|reflect|memory", "instruction": "self-contained text for this task", "stage": 1}}
  ],
  "reason": "one sentence explanation"
}}
"""


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _accumulate_usage(state: OrchestratorState, response: Any) -> None:
    """从 LangChain 响应累计 usage_metadata 到 state['_llm_tokens']。

    LangChain 的 AIMessage 带 usage_metadata（input_tokens/output_tokens），
    这是当前唯一真实的 token 数据来源；失败静默跳过，不影响主流程。
    并行子任务会并发累计 → 用锁保护 read-modify-write。
    """
    try:
        usage = getattr(response, "usage_metadata", None) or {}
        with _USAGE_LOCK:
            tokens = dict(state.get("_llm_tokens") or {})
            tokens["prompt"] = tokens.get("prompt", 0) + int(usage.get("input_tokens", 0) or 0)
            tokens["completion"] = tokens.get("completion", 0) + int(usage.get("output_tokens", 0) or 0)
            state["_llm_tokens"] = tokens
    except Exception:
        pass


_VALID_AGENTS = ("chatbot", "plan", "reflect", "memory")
_MAX_TASKS = 4


def planner_node(state: OrchestratorState) -> OrchestratorState:
    """LLM 分析意图，产出有序子任务列表（单意图 → 恰好 1 个任务，等价旧路由）。"""
    # conversation 可能是 list[str]（旧格式）或 list[dict]（新格式）
    raw_conv = state.get("conversation", []) or []
    if raw_conv and isinstance(raw_conv[0], dict):
        conv_text = "\n".join(
            f"{t.get('role', '?')}: {t.get('content', '')[:200]}"
            for t in raw_conv[-5:]
        ) or "(none)"
    else:
        conv_text = "\n".join(str(t)[:200] for t in raw_conv[-5:]) or "(none)"

    input_text = state.get("input_text", "")
    logger.info("orchestrator.planner", msg="分析用户意图，规划子任务...")
    prompt = PLANNER_PROMPT.format(
        input=input_text,
        conversation=conv_text,
    )
    model = get_chat_model(temperature=0.2)
    try:
        response = model.invoke(prompt)
        _accumulate_usage(state, response)
        text = response.content if hasattr(response, "content") else str(response)
        if text.startswith("```"):
            import re
            text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("` \n")
        data = json.loads(text)
        state["tasks"] = _parse_tasks(data, input_text)
        state["route_reason"] = data.get("reason", "")
    except Exception:
        state["tasks"] = [{"agent": "chatbot", "instruction": input_text}]
        state["route_reason"] = "fallback: parse error"
        logger.warning("orchestrator.planner_fallback", error="parse error")

    # 主路由 = 第一个任务（兼容 grader "路由到 X" 判定）
    tasks = state.get("tasks") or [{"agent": "chatbot", "instruction": input_text}]
    state["tasks"] = tasks
    state["route"] = tasks[0].get("agent", "chatbot")
    state["route_reason"] = state.get("route_reason", "")
    logger.info("orchestrator.route",
                route=state["route"],
                task_count=len(tasks),
                reason=state["route_reason"])
    return state


def _parse_tasks(data: dict, input_text: str) -> list[dict]:
    """解析 planner 输出。支持新格式 {"tasks": [...]} 与旧格式 {"route": "..."}。

    返回规范化后的任务列表（校验 agent 合法、instruction 非空、stage 合法、数量上限）。
    stage 缺失 → 按数组顺序递增（保守：全串行，行为与顺序执行一致）。
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        # 旧格式兼容：{"route": "chatbot"} → 单任务
        route = str(data.get("route", "chatbot") or "chatbot")
        route = route if route in _VALID_AGENTS else "chatbot"
        return [{"agent": route, "instruction": input_text, "stage": 1}]

    out: list[dict] = []
    for idx, t in enumerate(tasks[: _MAX_TASKS]):
        if not isinstance(t, dict):
            continue
        agent = str(t.get("agent", "") or "")
        if agent not in _VALID_AGENTS:
            agent = "chatbot"
        instruction = str(t.get("instruction", "") or "").strip()
        if not instruction:
            instruction = input_text
        stage = t.get("stage")
        try:
            stage = int(stage)
        except (TypeError, ValueError):
            stage = idx + 1  # 缺失/非法 → 按顺序（全串行，保守）
        if stage < 1:
            stage = idx + 1
        out.append({"agent": agent, "instruction": instruction, "stage": stage})
    if not out:
        return [{"agent": "chatbot", "instruction": input_text, "stage": 1}]
    return out


def _can_parallel_group(group: list[tuple[int, dict]]) -> bool:
    """静态安全兜底：组内并行条件。

    - 组内只有 1 个任务 → 无需并行
    - 含 chatbot → 串行（chatbot 共用同一 run_id 的 checkpoint，并行 invoke 会互相覆盖）
    - 其余组合（memory/plan/reflect 之间）→ 可并行（SQLite WAL + 线程本地连接安全）
    """
    if len(group) < 2:
        return False
    return not any(t.get("agent") == "chatbot" for _, t in group)


def execute_agent(state: OrchestratorState) -> OrchestratorState:
    """按 Planner 产出的任务列表顺序执行各子 Agent，最后 State Merge 汇总。"""
    user_id = state.get("user_id", "default_user")
    text = state.get("input_text", "")
    tasks = state.get("tasks") or [{"agent": state.get("route", "chatbot"), "instruction": text}]

    route = tasks[0].get("agent", "chatbot")
    route_labels = {
        "chatbot": "💬 对话回答",
        "plan": "📋 计划/任务",
        "reflect": "🔍 反思分析",
        "memory": "🧠 记忆处理",
    }
    logger.info("orchestrator.execute",
                route=route,
                task_count=len(tasks),
                step=f"开始执行 {len(tasks)} 个子任务 → 主路由 {route_labels.get(route, route)}")

    # 创建 Trace
    trace = TraceRecord(route, text[:100])
    state["trace_id"] = trace.trace_id  # run_id 保持线程 ID（会话身份），trace_id 单独记录
    from ..trace import set_current_trace
    set_current_trace(trace)

    def _step(event: str, msg: str, **extra) -> None:
        """同时写终端日志与 trace.step_log（环节序列, 供事后定位）。"""
        logger.info(event, step=msg, **extra)
        detail = ""
        if extra:
            try:
                detail = json.dumps(extra, ensure_ascii=False)[:300]
            except Exception:
                detail = str(extra)[:300]
        trace.add_step(msg, detail)

    task_results: list[dict] = [None] * len(tasks)  # 保序：按 index 填充
    reported: set[int] = set()
    all_ok = True

    try:
        trace.start()

        # 按 stage 分组：同 stage 并行，跨 stage 顺序（stage 缺失已在 _parse_tasks 归一为顺序）
        stages: dict[int, list[tuple[int, dict]]] = {}
        for i, task in enumerate(tasks):
            stages.setdefault(int(task.get("stage") or i + 1), []).append((i, task))

        for stage_no in sorted(stages):
            group = stages[stage_no]
            parallel = _can_parallel_group(group)
            _step("stage.start",
                  f"🚀 阶段 {stage_no}: {len(group)} 个任务" + ("（并行执行）" if parallel else ""))
            for _, task in group:
                _step("task.start",
                      f"🎯 子任务: [{task.get('agent', 'chatbot')}] {str(task.get('instruction') or text)[:60]}...")

            if parallel:
                with ThreadPoolExecutor(max_workers=min(len(group), _MAX_PARALLEL)) as ex:
                    future_map = {}
                    for i, task in group:
                        fut = ex.submit(
                            _execute_single_task, state, trace, _step,
                            task.get("agent", "chatbot"),
                            task.get("instruction") or text,
                            user_id,
                        )
                        future_map[fut] = (i, task)
                    for fut in as_completed(future_map):
                        i, task = future_map[fut]
                        tr = fut.result()  # _execute_single_task 内部捕获异常，不抛出
                        tr.setdefault("index", i + 1)
                        tr.setdefault("agent", task.get("agent", "chatbot"))
                        tr.setdefault("instruction", task.get("instruction") or text)
                        task_results[i] = tr
            else:
                for i, task in group:
                    tr = _execute_single_task(
                        state, trace, _step,
                        task.get("agent", "chatbot"),
                        task.get("instruction") or text,
                        user_id,
                    )
                    tr.setdefault("index", i + 1)
                    tr.setdefault("agent", task.get("agent", "chatbot"))
                    tr.setdefault("instruction", task.get("instruction") or text)
                    task_results[i] = tr

            # 汇总本阶段结果（按原任务顺序报告，保证日志可读）
            for i, tr in enumerate(task_results):
                if tr is None or i in reported:
                    continue
                reported.add(i)
                if tr.get("success"):
                    _step("task.done", f"✅ 子任务 {i + 1}: {tr.get('agent')} 完成")
                else:
                    all_ok = False
                    _step("task.failed",
                          f"❌ 子任务 {i + 1}: {tr.get('agent')} 失败: {str(tr.get('error', ''))[:80]}")

        state["task_results"] = task_results

        # ── State Merge：汇总所有子任务结果 ──
        if len(task_results) == 1:
            state["result"] = task_results[0].get("result", "")
            state["result_data"] = task_results[0].get("result_data", {}) or {}
        else:
            parts = []
            for tr in task_results:
                agent = tr.get("agent", "?")
                inst = str(tr.get("instruction", ""))[:60]
                body = tr.get("result", "") or f"（{agent} 无输出）"
                parts.append(f"【子任务 {tr.get('index', '?')}: {agent}】{inst}\n{body}")
            state["result"] = "\n\n".join(parts)
            state["result_data"] = {
                "merged_tasks": [
                    {"agent": t.get("agent"), "success": t.get("success"),
                     "result_data": t.get("result_data", {}) or {}}
                    for t in task_results
                ],
            }
            _step("state.merge", f"🧩 已合并 {len(task_results)} 个子任务结果")

        state["success"] = all_ok
        trace.final_output = state.get("result", "")[:500]
        trace.success = all_ok

        # 记录 LLM token 用量（planner + 子 agent 主路径的真实 usage）
        try:
            tk = state.get("_llm_tokens") or {}
            if tk.get("prompt") or tk.get("completion"):
                trace.set_llm_stats("deepseek-chat",
                                    prompt_tokens=tk.get("prompt", 0),
                                    completion_tokens=tk.get("completion", 0))
        except Exception:
            pass

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
        # 环节序列中记录异常（含堆栈前 400 字, 便于定位）
        try:
            import traceback as _tb
            tb_text = _tb.format_exc()[-400:]
            trace.add_step(f"❌ 执行失败: {error_msg}", tb_text)
        except Exception:
            trace.add_step(f"❌ 执行失败: {error_msg}")

    finally:
        set_current_trace(None)
        trace.stop()
        trace.save()

    return state


def _execute_single_task(state: OrchestratorState, trace: TraceRecord, _step,
                         agent: str, instruction: str, user_id: str) -> dict:
    """执行单个子任务（一个子 Agent 调用），异常不抛出，返回结构化结果。"""
    try:
        if agent == "plan":
            return _run_plan_task(state, trace, _step, instruction, user_id)
        if agent == "reflect":
            return _run_reflect_task(state, trace, _step, instruction, user_id)
        if agent == "memory":
            return _run_memory_task(state, trace, _step, instruction, user_id)
        return _run_chat_task(state, trace, _step, instruction, user_id)
    except Exception as exc:
        error_msg = str(exc)
        if "Error code:" in error_msg and len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."
        logger.error("task_exec_failed", agent=agent, error=error_msg)
        return {
            "success": False,
            "error": error_msg,
            "result": f"❌ {agent} 执行失败: {error_msg}",
            "result_data": {},
        }


def _run_plan_task(state, trace, _step, text: str, user_id: str) -> dict:
    """plan 子任务：区分计划生成与任务操作（原逻辑保持不变）。"""
    text_lower = text.lower()

    plan_keywords = ["生成.*计划", "写.*计划", "明天的计划", "今日计划",
                     "daily plan", "今天的计划"]
    is_plan_request = False
    import re
    for pk in plan_keywords:
        if re.search(pk, text_lower):
            is_plan_request = True
            break

    task_action_kw = ["添加", "删除", "删掉", "新建", "创建", "建一个",
                      "待办", "todo", "改成", "拆成", "拆分",
                      "merge", "合并", "标记", "状态", "强制",
                      "截止日期", "拆分"]
    is_task_op = any(kw in text_lower for kw in task_action_kw)

    if is_plan_request and not is_task_op:
        _step("plan.run", "📋 读取日记和记忆并生成计划...")
        r = run_plan_graph(user_id=user_id)
        items = r.get("items", [])
        if r.get("success"):
            lines = [f"📋 今日计划 ({r.get('date', '')})\n"]
            for i, item in enumerate(items, 1):
                src = item.get("source", "")
                icon = {"diary_todo": "📓", "pending_task": "🔄", "signal": "📡",
                        "stable_profile": "🎯", "default": "•"}.get(src, "•")
                lines.append(f"  {i}. {icon} [{item.get('priority', 'MEDIUM')}] {item.get('title', '')}")
            result = "\n".join(lines)
            _step("plan.complete", f"✅ 计划生成完成，共 {len(items)} 项")
        else:
            result = f"❌ 生成计划失败: {r.get('error', '')}"
            logger.error("plan.failed", error=r.get('error', ''))
        return {"success": bool(r.get("success")), "result": result,
                "result_data": r, "error": r.get("error") if not r.get("success") else None}
    if is_task_op:
        _step("plan.task_ops", "📋 执行任务操作...")
        from .plan_graph import run_task_ops
        r = run_task_ops(user_input=text, user_id=user_id)
        if r.get("success"):
            changes = r.get("changes", [])
            summary = r.get("summary", "")
            if changes:
                result = f"✅ 任务操作成功:\n" + "\n".join(f"  · {c}" for c in changes)
            else:
                result = f"ℹ️ {summary}"
            _step("plan.task_ops.done", f"✅ 任务操作完成: {summary[:80]}")
        else:
            result = f"❌ 任务操作失败: {r.get('error', '')}"
        return {"success": bool(r.get("success")), "result": result,
                "result_data": r, "error": r.get("error") if not r.get("success") else None}
    # 默认：生成计划
    _step("plan.run", "📋 读取日记和记忆并生成计划...")
    r = run_plan_graph(user_id=user_id)
    items = r.get("items", [])
    if r.get("success"):
        lines = [f"📋 今日计划 ({r.get('date', '')})\n"]
        for i, item in enumerate(items, 1):
            src = item.get("source", "")
            icon = {"diary_todo": "📓", "pending_task": "🔄", "signal": "📡",
                    "stable_profile": "🎯", "default": "•"}.get(src, "•")
            lines.append(f"  {i}. {icon} [{item.get('priority', 'MEDIUM')}] {item.get('title', '')}")
        result = "\n".join(lines)
        _step("plan.complete", f"✅ 计划生成完成，共 {len(items)} 项")
    else:
        result = f"❌ 生成计划失败: {r.get('error', '')}"
        logger.error("plan.failed", error=r.get('error', ''))
    return {"success": bool(r.get("success")), "result": result,
            "result_data": r, "error": r.get("error") if not r.get("success") else None}


def _run_reflect_task(state, trace, _step, text: str, user_id: str) -> dict:
    """reflect 子任务。"""
    _step("reflect.run", f"🔍 开始反思分析: {text[:60]}...")
    r = run_reflect(subject="diary_or_note", content=text, user_id=user_id)
    if r.get("success"):
        parts = [
            f"🔍 分析:\n{r.get('analysis', '')}\n",
            f"💡 批判:\n{r.get('critique', '')}\n",
            f"📌 建议:\n{r.get('suggestions', '')}\n",
            f"📝 总结:\n{r.get('summary', '')}",
        ]
        result = "\n".join(parts)
        _step("reflect.complete", "✅ 反思分析完成")
    else:
        result = f"❌ 反思失败: {r.get('error', '')}"
        logger.error("reflect.failed", error=r.get('error', ''))
    return {"success": bool(r.get("success")), "result": result,
            "result_data": r, "error": r.get("error") if not r.get("success") else None}


def _run_memory_task(state, trace, _step, text: str, user_id: str) -> dict:
    """memory 子任务。"""
    _step("memory.run", f"🧠 判断是否需要记忆: {text[:60]}...")
    r = run_memory_agent(trigger_text=text, user_id=user_id)
    result = r.get("summary", "记忆处理完成")
    if r.get("decision") in ("write", "update"):
        trace.add_memory_update("episodic", result[:100])
        _step("memory.saved", f"✅ 已保存记忆: {result}")
    else:
        _step("memory.skip", "⏭️ 无需记忆，已跳过")
    return {"success": bool(r.get("success", True)), "result": result,
            "result_data": r, "error": r.get("error") if not r.get("success", True) else None}


def _run_chat_task(state, trace, _step, text: str, user_id: str) -> dict:
    """chatbot 子任务。"""
    _step("chatbot.run", f"💬 构建上下文并回答: {text[:60]}...")
    from .chatbot_graph import build_chatbot_graph
    from langchain_core.messages import HumanMessage, AIMessage
    from ..agent_data_service import build_context

    # 先构建上下文并记录到 trace
    ctx = build_context(task=text, session_id=state.get("run_id", ""))
    for s in ctx.get("sources", []):
        trace.add_context_source(
            s.get("layer", "?"), s.get("type", "?"),
            s.get("chars", 0),
        )

    graph = build_chatbot_graph()

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
                _step("context.pressure",
                      format_pressure_line(press),
                      compressed=len(compressed) < len(conv),
                      turns=f"{len(conv)}→{len(compressed)}")
                conv = compressed
                press["compressed"] = len(compressed) < len(conv)
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
                try:
                    content = str(turn)
                    role = "human"
                except Exception:
                    continue

            if role == "human" or role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "ai" or role == "assistant":
                if len(content) > 1200:
                    content = content[:800] + "\n\n...(省略中间)...\n\n" + content[-400:]
                messages.append(AIMessage(content=content))
            else:
                messages.append(HumanMessage(content=content))

    messages.append(HumanMessage(content=text))

    chat_thread = f"chat_{state.get('run_id') or uuid.uuid4().hex[:10]}"
    config = {"configurable": {"thread_id": chat_thread}}
    response = graph.invoke(
        {"messages": messages},
        config,
    )
    last_msg = response["messages"][-1]
    _accumulate_usage(state, last_msg)
    result = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    _step("chatbot.complete", "✅ 回答完成", message_count=len(response["messages"]))
    return {"success": True, "result": result,
            "result_data": {"message_count": len(response["messages"])}, "error": None}


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

_graph = None


def build_orchestrator():
    global _graph
    if _graph is not None:
        return _graph

    builder = StateGraph(OrchestratorState)

    builder.add_node("planner", planner_node)
    builder.add_node("execute", execute_agent)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "execute")
    builder.add_edge("execute", END)

    # 2026-08 改造: orchestrator 每次 invoke 都传入完整 initial state,
    # checkpointer 对它无意义 (每次全量覆盖), 移除避免误导。
    _graph = builder.compile()
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
        "tasks": [],
        "task_results": [],
        "result": "",
        "result_data": {},
        "success": True,
        "error": None,
        "run_id": run_id,
        "trace_id": None,
        "_llm_tokens": {},
        "step_log": [],
    }

    try:
        # orchestrator 图无 checkpointer (每次全量 initial state), 无需 config
        result = graph.invoke(initial)
        latency = int((time.monotonic() - start) * 1000)
        route = result.get("route", "?")
        success = result.get("success", False)
        task_count = len(result.get("tasks") or [])
        logger.info("orchestrator.done",
                    step=f"✅ 执行完毕 (route={route}, tasks={task_count})",
                    latency=f"{latency}ms",
                    success=success)

        # ── 自进化自动触发（节流 + 后台，不阻塞响应） ──
        try:
            from ..evolution.runner import maybe_auto_evolve
            evo = maybe_auto_evolve()
            if evo.get("triggered"):
                logger.info("orchestrator.evolve",
                            step="🧬 自进化已触发（后台蒸馏+策略更新）",
                            undistilled=evo.get("undistilled"))
        except Exception:
            pass

        return {
            "success": result.get("success", False),
            "route": result.get("route", "?"),
            "route_reason": result.get("route_reason", ""),
            "result": result.get("result", ""),
            "result_data": result.get("result_data", {}),
            "tasks": result.get("tasks", []),
            "task_results": result.get("task_results", []),
            "latency_ms": latency,
            "run_id": result.get("run_id", ""),
            "trace_id": result.get("trace_id", ""),
        }
    except Exception as exc:
        logger.error("orchestrator.failed", step="❌ Orchestrator 执行失败", error=str(exc))
        return {"success": False, "error": str(exc), "route": "error", "result": str(exc)}
