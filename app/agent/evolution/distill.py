"""Distill — L1 经验蒸馏：把原始 trace 提炼为经验与策略建议。

reflect 的批量模式：
  - 输入：上次蒸馏之后积累的 trace（紧凑摘要）
  - 输出（LLM 一次调用）：
      1. experiences      → 写 lessons.md / decisions.md（topic memory）+ episodic
      2. policy_suggestions → 交给 update.apply_policy_suggestions（L2 应用）
  - 自身不写 trace（避免自增蒸馏计数），只推进 state.json 的蒸馏游标。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.agent.graphs.llm import get_chat_model
from app.core.logging import logger

from . import experience as exp

DISTILL_PROMPT = """You are the reflection engine of a personal AI agent. Distill raw execution traces into durable experience.

## Input: recent task executions (newest last)
{traces}

## Task
Analyze patterns: repeated tasks, recurring mistakes, inefficiencies (slow paths, unnecessary tool calls, repeated clarification), and what worked well.

## Output — JSON only:
{{
  "experiences": [
    {{
      "title": "short title",
      "category": "lessons|decisions",
      "content": "one or two sentences, concrete and actionable",
      "tags": ["tag1"]
    }}
  ],
  "policy_suggestions": [
    {{
      "task_type": "chatbot|plan|reflect|memory|daily_plan",
      "trigger": "keyword or condition that should activate this policy",
      "action": "exact behavioral change, one sentence, imperative",
      "benefit": "expected improvement: latency/tokens/success"
    }}
  ]
}}

Rules:
- experiences: at most 4. Prefer NEW insights; skip trivia.
- policy_suggestions: at most 2, only when the traces show a clear repeated pattern with a concrete fix. Each must be a change the agent can actually follow next time.
- Do not invent metrics. Use only what the traces show.
"""


def _compact_trace(t: dict[str, Any]) -> str:
    tools = ", ".join(t["tool_names"]) if t["tool_names"] else "-"
    return (f"[{t['timestamp'][:16]}] {t['task_type']} | intent={t['intent'][:60]} | "
            f"latency={t['latency_ms']}ms tokens={t['total_tokens']} "
            f"tools=({tools}) | {'ok' if t['success'] else 'FAIL: ' + t['error'][:80]}")


def _parse_llm_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned).rstrip("` \n")
    return json.loads(cleaned)


def distill_once(force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """蒸馏一批未处理的 trace。返回报告 dict。"""
    traces = exp.load_traces(limit=200)
    state = exp.load_state()
    undistilled = exp.get_undistilled(traces, state)

    if not force and len(undistilled) < exp.MIN_DISTILL_TRACES:
        return {"skipped": True,
                "reason": f"未蒸馏 trace 仅 {len(undistilled)} 条（阈值 {exp.MIN_DISTILL_TRACES}）",
                "undistilled": len(undistilled)}

    batch = undistilled[:exp.MAX_DISTILL_INPUT]  # 正序：最旧的先处理，逐批消化积压
    prompt = DISTILL_PROMPT.format(
        traces="\n".join(_compact_trace(t) for t in batch))

    if dry_run:
        return {"dry_run": True, "undistilled": len(undistilled),
                "batch_size": len(batch),
                "prompt_preview": prompt[:500]}

    try:
        model = get_chat_model(temperature=0.3)
        response = model.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        data = _parse_llm_json(text)
    except Exception as exc:
        logger.error("evolve.distill.llm_failed", error=str(exc)[:200])
        return {"success": False, "error": f"LLM 蒸馏失败: {exc}"}

    experiences = data.get("experiences", []) or []
    suggestions = data.get("policy_suggestions", []) or []

    applied = _apply_experiences(experiences)
    policy_result = None
    if suggestions:
        try:
            from . import update
            policy_result = update.apply_policy_suggestions(suggestions)
        except Exception as exc:
            logger.error("evolve.distill.policy_failed", error=str(exc)[:200])
            policy_result = {"success": False, "error": str(exc)}

    # 推进蒸馏游标：推进到本批中最新的时间戳（本批已全部处理，
    # 更旧的积压 trace 下次继续处理，不会被跳过）
    new_cursor = max(t["ts"] for t in batch).isoformat()
    state["last_distilled_at"] = new_cursor
    state["distill_count"] = int(state.get("distill_count", 0)) + 1
    exp.save_state(state)

    return {
        "success": True,
        "undistilled": len(undistilled),
        "batch_size": len(batch),
        "experiences": len(experiences),
        "policy_suggestions": len(suggestions),
        "applied": applied,
        "policy_result": policy_result,
        "cursor": new_cursor,
    }


def _apply_experiences(experiences: list[dict]) -> dict[str, Any]:
    """经验落库：episodic + lessons.md/decisions.md + MEMORY.md 索引。"""
    from ..agent_data_service import add_episodic
    from ..topic_memory import upsert_index_entry, write_topic

    stats = {"episodic": 0, "lessons": 0, "decisions": 0}
    for ex in experiences:
        title = str(ex.get("title", ""))[:80]
        content = str(ex.get("content", ""))[:500]
        category = "lessons" if ex.get("category") != "decisions" else "decisions"
        tags = [str(x) for x in (ex.get("tags") or [])][:4]
        if not title or not content:
            continue

        try:
            add_episodic(f"[经验] {title}: {content}", tags=["experience"] + tags)
            stats["episodic"] += 1
        except Exception:
            pass

        try:
            entry = f"- {title}: {content}"
            if tags:
                entry += f"（{'/'.join(tags)}）"
            write_topic(category, f"## {datetime.now().isoformat()[:10]} {title}\n{content}\n",
                        append=True)
            upsert_index_entry(
                "Lessons" if category == "lessons" else "Decisions",
                title, category, content[:80],
                related=tags,
            )
            stats[category] += 1
        except Exception as exc:
            logger.error("evolve.distill.write_failed", error=str(exc)[:200])

    return stats
