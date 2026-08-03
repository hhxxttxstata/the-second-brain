"""一键捕获 — 将不满意的 Agent 输出快速存入 candidate 评测集。

用法:
    from app.agent.capture import capture_from_trace, prompt_tags

流程:
    1. 用户觉得 Agent 回答不对
    2. 输入 /report 或 python -m app.cli report
    3. 自动拉取最近的 trace + 输出 → candidate 用例
    4. 提示用户补 tags 和备注（可选）
    5. 存到 agent_data/eval/candidate/{timestamp}.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.trace import get_latest_trace
from app.core.config import settings

_CANDIDATE_DIR = settings.agent_data_dir / "eval" / "candidate"


def _ensure_dir() -> Path:
    _CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    return _CANDIDATE_DIR


def capture_from_trace(
    tags: list[str] | None = None,
    note: str = "",
    intent: str = "",
) -> dict[str, Any]:
    """从最近的 trace 捕获一条 candidate 测试用例。

    Args:
        tags: 能力标签（可选，会提示用户补充）
        note: 用户备注（可选）
        intent: 简短意图描述（可选，自动从 trace 推断）

    Returns:
        {"success": True, "file": "...", "case": {...}}
    """
    trace = get_latest_trace()
    if not trace:
        return {"success": False, "error": "没有找到最近的 trace 记录"}

    trace_id = trace.get("trace_id", "?")
    user_intent = intent or trace.get("user_intent", "") or ""
    input_text = trace.get("user_intent", "") or ""
    final_output = trace.get("final_output", "") or ""
    route = trace.get("task_type", "?")
    tool_calls = trace.get("tool_calls", [])

    # 自动推断 intent
    if not intent and input_text:
        user_intent = input_text[:50]

    case: dict[str, Any] = {
        "id": f"candidate-captured-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "intent": user_intent[:40],
        "input": input_text,
        "expected_route": route,
        "stage": "candidate",
        "tags": tags or [],
        "note": note or f"自动捕获自 trace {trace_id}",
        "created_at": datetime.now().isoformat()[:10],
        "substage": None,
        "known_issue": f"用户反馈该回答不满足预期。trace: {trace_id}",
        "trace_ref": trace_id,
        "trace_snapshot": {
            "tool_calls": len(tool_calls),
            "tool_success_rate": _calc_tool_success_rate(tool_calls),
            "has_error": bool(trace.get("error")),
            "error": (trace.get("error") or "")[:200],
            "output_preview": final_output[:300],
        },
    }

    return {"success": True, "case": case, "trace_id": trace_id}


def _calc_tool_success_rate(tool_calls: list[dict]) -> float:
    if not tool_calls:
        return 0.0
    success = sum(1 for tc in tool_calls if tc.get("success"))
    return round(success / len(tool_calls), 2)


def prompt_tags() -> list[str]:
    """收集用户补充的标签。"""
    print()
    print("  📋 可选的标签（逗号分隔，直接回车跳过）:")
    print("     routing, memory_read, memory_write, vault_search, planning,")
    print("     reflection, self_intro, contradiction, multi_turn, external_api,")
    print("     ambiguous, fixture_dependent, tool_reliability, hallucination")
    try:
        raw = input("  标签 > ").strip()
        if raw:
            return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]
    except (EOFError, KeyboardInterrupt):
        pass
    return []


def prompt_note() -> str:
    """收集用户的简短备注。"""
    try:
        note = input("  备注（为什么不满意？可回车跳过） > ").strip()
        return note
    except (EOFError, KeyboardInterrupt):
        return ""


def save_candidate(case: dict[str, Any]) -> str:
    """将 candidate case 写入文件，返回文件路径。"""
    _ensure_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _CANDIDATE_DIR / f"captured_{ts}.json"
    path.write_text(
        json.dumps([case], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


def print_case_preview(case: dict[str, Any]) -> None:
    """打印待保存的 candidate 预览。"""
    print(f"\n  捕获的候选用例预览:")
    print(f"    intent:  {case.get('intent', '?')}")
    print(f"    input:   {case.get('input', '?')[:60]}")
    print(f"    路由:    {case.get('expected_route', '?')}")
    print(f"    标签:    {', '.join(case.get('tags', []) or ['(无)'])}")
    ts = case.get("trace_snapshot", {})
    print(f"    工具:    {ts.get('tool_calls', '?')} 次调用, "
          f"成功率 {ts.get('tool_success_rate', '?')}")
    if ts.get("error"):
        print(f"    error:   {ts['error'][:80]}")
    output = case.get("trace_snapshot", {}).get("output_preview", "")
    if output:
        print(f"    输出片段: {output[:120]}")
