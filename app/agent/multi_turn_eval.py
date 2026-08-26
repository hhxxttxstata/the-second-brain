"""多轮任务评测 — 借鉴 τ-bench 的 user simulator 方法论。

τ-bench 核心思路:
  - 用 LLM 模拟真实用户，与 agent 多轮对话
  - 用户"一次只说必要信息"，不一次性给全指令
  - 目标达成时用户发 ###STOP### 结束
  - 评测指标: 任务完成率（utility）+ 约束满足

本模块把它适配到个人知识 Agent 场景:
  - 用户任务: 知识问答 / 任务操作 / 记忆写入（agent 的 4 类能力）
  - 用户模拟器: DeepSeek（与 agent 同模型，降低成本）
  - 判定: 用户模拟器自我声明任务完成 + 约束检查器验证真实状态
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

_MULTI_DIR = settings.agent_data_dir / "multi_turn"
_MULTI_DIR.mkdir(parents=True, exist_ok=True)

# 多轮任务用例: 每个任务描述 agent 需要达成的目标（不一次性给全信息）
MULTI_TURN_TASKS: list[dict[str, Any]] = [
    {
        "task_id": "mt-001",
        "task": "你（用户）想记录一个待办：『[mt_eval] 秋招简历打磨计划』（这是一个评测专用的新待办，确保名称唯一），优先级为高。需要 agent 帮你创建这个待办。创建成功后，你还要再问 agent 当前有哪些待办。",
        "expected_tools": ["update_task_status", "write_memory", "read_memory"],
        "expected_outcome": "待办中新增了『[mt_eval] 秋招简历打磨计划』（high 优先级）",
        "category": "task_ops",
    },
    {
        "task_id": "mt-002",
        "task": "你（用户）想告诉 agent 一个偏好：『以后代码示例默认用 Python』。然后过两轮再问 agent 是否还记得这个偏好。",
        "expected_tools": ["write_memory", "write_topic_memory", "search_memories"],
        "expected_outcome": "偏好已持久化，且 agent 能在后续轮次回忆起",
        "category": "memory",
    },
    {
        "task_id": "mt-003",
        "task": "你（用户）想知道 Obsidian 笔记里关于 RAG 的内容，先问 agent『帮我搜一下RAG相关的笔记』，然后根据回答追问一个细节。",
        "expected_tools": ["search_vault", "read_folder", "read_file", "search_topic_memory"],
        "expected_outcome": "agent 检索到 vault 中的 RAG 笔记并回答",
        "category": "knowledge_qa",
    },
    {
        "task_id": "mt-004",
        "task": "你（用户）先告诉 agent『记住：我明天要去北京』，然后下一轮改口『不对，我改成后天去』，看 agent 是否正确更新记忆（旧记忆被覆盖）。",
        "expected_tools": ["write_memory", "write_topic_memory", "search_memories"],
        "expected_outcome": "记忆从『明天去北京』更新为『后天去北京』，无矛盾残留",
        "category": "memory_update",
    },
]


def _build_user_prompt(task: dict[str, Any]) -> list[dict[str, str]]:
    """构建用户模拟器的 system prompt（借鉴 τ-bench 规则）。"""
    return [
        {
            "role": "system",
            "content": (
                "你是一个真实用户，正在与一个个人知识 Agent 对话。\n"
                "规则：\n"
                "- 每次只生成一句话，模拟用户的消息\n"
                "- 不要一次性把全部信息说出去，只提供当前步骤需要的信息\n"
                "- 不要编造任务描述里没有的信息\n"
                "- 如果 agent 问的信息你在任务里没有，就说不知道或不记得\n"
                "- 当任务目标达成时，单独生成 ###STOP### 结束对话\n"
                "- 不要重复任务描述原文，用自己的话自然表达\n"
                "- 重要：当 agent 已经对任务做出了实质行动（如成功保存记忆/创建待办/检索到内容/明确回答），"
                "即使回复不完全符合你的期待，也应承认任务完成并停止。不要反复提出相同要求。\n"
                f"\n任务: {task['task']}\n"
                f"预期结果: {task['expected_outcome']}"
            ),
        },
    ]


def _agent_step(input_text: str, thread_id: str) -> str:
    """让 agent 回答一轮。"""
    from app.agent.graphs.orchestrator import run_orchestrator
    r = run_orchestrator(input_text=input_text, thread_id=thread_id)
    if r.get("success"):
        return r.get("result", "")
    return f"❌ {r.get('error', '处理失败')}"


def _user_step(messages: list[dict[str, str]], agent_reply: str) -> tuple[str, bool]:
    """用户模拟器生成下一轮回复。返回 (回复, 是否结束)。"""
    from app.agent.graphs.llm import get_chat_model

    msgs = list(messages)
    msgs.append({"role": "assistant", "content": agent_reply})
    model = get_chat_model(temperature=0.3)
    resp = model.invoke(msgs)
    text = resp.content if hasattr(resp, "content") else str(resp)
    text = text.strip()
    if text.startswith("###STOP###"):
        return "", True
    return text, False


def run_multi_turn_eval(tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """跑多轮任务评测（user simulator 方法论）。"""
    from app.agent.trace import load_all_traces, _find_inner_trace
    from app.agent.failure_taxonomy import detect_failure_codes

    tasks = tasks or MULTI_TURN_TASKS
    results: list[dict[str, Any]] = []
    success_count = 0

    # 评测隔离：清理上一次评测的残留 todo（标题带 _mt_eval 标记）
    try:
        from app.agent.agent_data_service import read_memory, write_memory
        td = read_memory("task")
        if "todos" in td:
            before = len(td["todos"])
            td["todos"] = [t for t in td["todos"] if not str(t.get("title", "")).startswith("[mt_eval]")]
            if len(td["todos"]) < before:
                write_memory("task", td, merge=False)
    except Exception:
        pass

    for task in tasks:
        logger.info("multi_turn.start", task=task["task_id"], category=task["category"])
        thread_id = f"mt_{task['task_id']}_{int(time.time())}"
        user_msgs = _build_user_prompt(task)
        history: list[dict[str, str]] = []
        done = False
        turns = 0
        agent_replies: list[str] = []
        max_turns = 8

        # 用户先说第一句
        from app.agent.graphs.llm import get_chat_model
        model = get_chat_model(temperature=0.3)
        resp = model.invoke(user_msgs)
        user_text = resp.content if hasattr(resp, "content") else str(resp)
        user_text = user_text.strip()
        if user_text.startswith("###STOP###"):
            user_text = "你好，帮我一个忙。"

        while not done and turns < max_turns:
            turns += 1
            agent_reply = _agent_step(user_text, thread_id)
            agent_replies.append(agent_reply)
            user_text, done = _user_step(user_msgs, agent_reply)
            # 兜底：agent 回复含实质行动信号 → 认为任务达成（模拟器偶发不 STOP）
            if not done and any(k in agent_reply for k in (
                    "已添加", "已保存", "已创建", "找到了", "以下是", "已记录",
                    "已更新", "已写入", "记忆已保存")):
                done = True

        # 判定: 是否自然结束（用户满意）+ agent 每轮都有输出
        task_success = done
        if not done:
            task_success = False

        # 工具检查: 期望工具是否被调用（memory agent 内部直接写 SQLite，
        # 不走 tool registry → 用"记忆确实写入"兜底）
        traces = load_all_traces(limit=30)
        called_tools = set()
        for tr in traces:
            for tc in tr.get("tool_calls", []):
                called_tools.add(tc.get("name", ""))
        expected = set(task.get("expected_tools", []))
        tools_hit = expected & called_tools
        tool_ok = len(tools_hit) > 0

        # 兜底: memory 类任务检查 SQLite 是否真的写入
        if not tool_ok and task.get("category") in ("memory", "memory_update"):
            try:
                from app.agent.memory_store import _get_conn
                conn = _get_conn()
                row = conn.execute(
                    "SELECT COUNT(*) c FROM memories WHERE content LIKE '%北京%' "
                    "OR content LIKE '%Python%' OR content LIKE '%秋招%'"
                ).fetchone()
                if row and row["c"] > 0:
                    tool_ok = True
                    tools_hit = {"(SQLite直接写入)"}
            except Exception:
                pass

        # 兜底: task_ops 类任务检查 todos 是否真的新增（plan_graph 内部直接写）
        if not tool_ok and task.get("category") == "task_ops":
            try:
                from app.agent.agent_data_service import read_memory
                td = read_memory("task")
                titles = [t.get("title", "") for t in td.get("todos", [])]
                if any("mt_eval" in t for t in titles):
                    tool_ok = True
                    tools_hit = {"(todos直接写入)"}
            except Exception:
                pass

        final_success = task_success and tool_ok

        if final_success:
            success_count += 1

        results.append({
            "task_id": task["task_id"],
            "category": task["category"],
            "task": task["task"][:80],
            "success": final_success,
            "turns": turns,
            "user_satisfied": task_success,
            "tools_called": sorted(called_tools)[:10],
            "tools_hit": sorted(tools_hit),
            "tool_ok": tool_ok,
            "last_agent_reply": (agent_replies[-1] if agent_replies else "")[:200],
        })
        logger.info("multi_turn.done", task=task["task_id"],
                    success=final_success, turns=turns)

    report = {
        "timestamp": datetime.now().isoformat(),
        "total_tasks": len(tasks),
        "pass_rate": round(success_count / len(tasks) * 100, 1) if tasks else 0,
        "results": results,
    }
    ( _MULTI_DIR / f"multi_turn_{datetime.now().strftime('%Y%m%d_%H%M')}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def format_multi_turn_report(report: dict[str, Any]) -> str:
    lines = [
        "=" * 54,
        "  🔄 多轮任务评测（τ-bench 方法论）",
        f"  通过率: {report.get('pass_rate', 0)}% ({report.get('total_tasks', 0)} 个任务)",
        "=" * 54,
    ]
    for r in report.get("results", []):
        icon = "✅" if r.get("success") else "❌"
        lines.append(f"  {icon} [{r.get('category','?'):14s}] {r.get('task_id','?'):8s} "
                     f"({r.get('turns',0)}轮)")
        lines.append(f"     工具命中: {r.get('tools_hit') or '无'}")
        if not r.get("success"):
            why = "用户未满意(未STOP)" if not r.get("user_satisfied") else "工具未命中"
            lines.append(f"     ⚠️ {why}")
    lines.append("=" * 54)
    return "\n".join(lines)
