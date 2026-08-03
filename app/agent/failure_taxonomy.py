"""Agent Failure Taxonomy — 统一失败分类系统。

每个失败映射到一个标准错误码，支持：
- 自动检测：从 trace 记录推断失败码
- 手动标注：case 定义时预置 expected_failure
- 聚合分析：按类型/任务/版本分布

错误码总表（3 大类 15 种）：
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ===========================================================================
# 错误码定义
# ===========================================================================

# -- R: 路由与意图 --
ROUTING_ERROR = "ROUTING_ERROR"
MISSING_CRITICAL_EVIDENCE = "MISSING_CRITICAL_EVIDENCE"
IRRELEVANT_CONTEXT = "IRRELEVANT_CONTEXT"
UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
APPROVAL_BYPASS = "APPROVAL_BYPASS"
CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"

# -- T: 工具与执行 --
WRONG_TOOL = "WRONG_TOOL"
WRONG_TOOL_ARGUMENT = "WRONG_TOOL_ARGUMENT"
FALSE_COMPLETION = "FALSE_COMPLETION"
DUPLICATE_SIDE_EFFECT = "DUPLICATE_SIDE_EFFECT"
TASK_STATE_LOST = "TASK_STATE_LOST"
TOOL_RECOVERY_FAILED = "TOOL_RECOVERY_FAILED"

# -- M: 记忆与数据 --
MEMORY_WRITE_FALSE_POSITIVE = "MEMORY_WRITE_FALSE_POSITIVE"
MEMORY_RECALL_MISS = "MEMORY_RECALL_MISS"
MEMORY_CONFLICT_NOT_RESOLVED = "MEMORY_CONFLICT_NOT_RESOLVED"

# -- 聚合分组 --
ALL_FAILURE_CODES: list[str] = [
    ROUTING_ERROR,
    MISSING_CRITICAL_EVIDENCE,
    IRRELEVANT_CONTEXT,
    UNSUPPORTED_CLAIM,
    APPROVAL_BYPASS,
    CONTEXT_OVERFLOW,
    WRONG_TOOL,
    WRONG_TOOL_ARGUMENT,
    FALSE_COMPLETION,
    DUPLICATE_SIDE_EFFECT,
    TASK_STATE_LOST,
    TOOL_RECOVERY_FAILED,
    MEMORY_WRITE_FALSE_POSITIVE,
    MEMORY_RECALL_MISS,
    MEMORY_CONFLICT_NOT_RESOLVED,
]

FAILURE_CATEGORIES: dict[str, str] = {
    ROUTING_ERROR: "路由与意图",
    MISSING_CRITICAL_EVIDENCE: "路由与意图",
    IRRELEVANT_CONTEXT: "路由与意图",
    UNSUPPORTED_CLAIM: "路由与意图",
    APPROVAL_BYPASS: "路由与意图",
    CONTEXT_OVERFLOW: "路由与意图",
    WRONG_TOOL: "工具与执行",
    WRONG_TOOL_ARGUMENT: "工具与执行",
    FALSE_COMPLETION: "工具与执行",
    DUPLICATE_SIDE_EFFECT: "工具与执行",
    TASK_STATE_LOST: "工具与执行",
    TOOL_RECOVERY_FAILED: "工具与执行",
    MEMORY_WRITE_FALSE_POSITIVE: "记忆与数据",
    MEMORY_RECALL_MISS: "记忆与数据",
    MEMORY_CONFLICT_NOT_RESOLVED: "记忆与数据",
}

FAILURE_DESCRIPTIONS: dict[str, str] = {
    ROUTING_ERROR: "路由到错误的子 Agent",
    MISSING_CRITICAL_EVIDENCE: "未检索到必需的关键证据",
    IRRELEVANT_CONTEXT: "上下文引用与问题无关",
    UNSUPPORTED_CLAIM: "输出来源不明的断言",
    APPROVAL_BYPASS: "绕过必需的审批流程",
    CONTEXT_OVERFLOW: "上下文超过 Token 预算导致退化",
    WRONG_TOOL: "选择了错误的工具",
    WRONG_TOOL_ARGUMENT: "工具的关键参数错误",
    FALSE_COMPLETION: "声称完成但实际未完成",
    DUPLICATE_SIDE_EFFECT: "重复执行了幂等操作",
    TASK_STATE_LOST: "中途丢失了任务状态",
    TOOL_RECOVERY_FAILED: "工具失败后无法优雅恢复",
    MEMORY_WRITE_FALSE_POSITIVE: "将瞬时状态写入长期记忆",
    MEMORY_RECALL_MISS: "存在相关记忆但未能召回",
    MEMORY_CONFLICT_NOT_RESOLVED: "新旧冲突记忆未检测或未覆盖",
}

FAILURE_SEVERITY: dict[str, str] = {
    ROUTING_ERROR: "high",
    MISSING_CRITICAL_EVIDENCE: "high",
    IRRELEVANT_CONTEXT: "medium",
    UNSUPPORTED_CLAIM: "critical",
    APPROVAL_BYPASS: "critical",
    CONTEXT_OVERFLOW: "medium",
    WRONG_TOOL: "high",
    WRONG_TOOL_ARGUMENT: "high",
    FALSE_COMPLETION: "critical",
    DUPLICATE_SIDE_EFFECT: "high",
    TASK_STATE_LOST: "high",
    TOOL_RECOVERY_FAILED: "medium",
    MEMORY_WRITE_FALSE_POSITIVE: "medium",
    MEMORY_RECALL_MISS: "high",
    MEMORY_CONFLICT_NOT_RESOLVED: "medium",
}

# test case 可以预置期望的错误码
# golden 晋升要求 ≥1 个 failure_code
# 即：一个 golden case 必须至少知道它会暴露什么类型的失败


# ===========================================================================
# 自动检测 — 从 trace 记录推断失败码
# ===========================================================================

# 语义关键词 → 失败码（用于 final_output 文本匹配）
_OUTPUT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"已\s*(?:保存|记录)"), FALSE_COMPLETION),
    (re.compile(r"已经\s*记住"), FALSE_COMPLETION),
    (re.compile(r"成功(?:\s*(?:保存|记录|完成|了))"), FALSE_COMPLETION),
    (re.compile(r"已完成"), FALSE_COMPLETION),
]

# 工具错误关键词 → 失败码
_TOOL_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:需要审批|拒绝|不允许|no permission|unauthorized|approval required)", re.I), APPROVAL_BYPASS),
    (re.compile(r"(?:关键参数|required.*missing|missing.*required|validation.*fail)", re.I), WRONG_TOOL_ARGUMENT),
    (re.compile(r"(?:rate limit|timeout|connection error|500|503)", re.I), TOOL_RECOVERY_FAILED),
    (re.compile(r"(?:重复.*执行|已经.*操作|重复提交|duplicate|idempotency)", re.I), DUPLICATE_SIDE_EFFECT),
]


def detect_failure_codes(trace: dict[str, Any]) -> list[str]:
    """从 trace 记录自动推断失败码。

    根据现有的 trace 字段（task_type、tool_calls、final_output、error、memory_updates）
    做多规则判断。
    """
    codes: list[str] = []

    task_type = trace.get("task_type", "")
    tool_calls = trace.get("tool_calls", [])
    final_output = (trace.get("final_output") or "")
    error = trace.get("error") or ""
    success = trace.get("success", True)
    memory_updates = trace.get("memory_updates", [])

    # 1. 工具调用失败检测（不依赖 success 字段，看 tool_calls 本身）
    for tc in tool_calls:
        tc_success = tc.get("success", True)
        tc_error = tc.get("error") or ""

        if tc_success and not tc_error:
            continue

        if not tc_success:
            if TOOL_RECOVERY_FAILED not in codes:
                codes.append(TOOL_RECOVERY_FAILED)

        for pat, code in _TOOL_ERROR_PATTERNS:
            if pat.search(tc_error):
                if code not in codes:
                    codes.append(code)

    # 4. T 类：FALSE_COMPLETION
    # 检出一个工具也没调但输出声称已完成的
    has_save_claim = FALSE_COMPLETION not in codes  # 还没被推过
    if has_save_claim:
        output_text = (final_output or "")
        for pat, code in _OUTPUT_PATTERNS:
            if code == FALSE_COMPLETION and pat.search(output_text):
                if task_type in ("memory", "plan", "daily_plan"):
                    # 这类 task 调了工具才算真的完成
                    relevant_tool_names = ("write_memory", "write_episodic_memory",
                                           "update_task_status", "write_episodic",
                                           "save_task")
                    has_relevant_tool = any(
                        tc.get("name") in relevant_tool_names
                        for tc in tool_calls
                    )
                    if not has_relevant_tool and not memory_updates:
                        codes.append(FALSE_COMPLETION)
                break

    # 5. M 类：MEMORY_WRITE_FALSE_POSITIVE
    # 检测典型的"把情绪/临时状态当成长期事实写入"
    if task_type == "chatbot":
        output_text = (final_output or "")
        emotion_markers = ["头痛", "累了", "困了", "不舒服", "烦躁", "开心", "难过",
                           "生气", "郁闷", "今天", "现在", "目前", "最近"]
        has_emotion = any(m in output_text for m in emotion_markers)
        if has_emotion and memory_updates:
            for mu in memory_updates:
                content = (mu.get("preview") or mu.get("content") or "").lower()
                if any(m in content for m in emotion_markers):
                    codes.append(MEMORY_WRITE_FALSE_POSITIVE)
                    break

    # 6. R 类：ROUTING_ERROR
    # 通过 expected_route vs actual route 判断（需要 case 定义预置 expected_route）
    # 这里不做，在 benchmark grader 中判断

    # 7. R 类：UNSUPPORTED_CLAIM
    # 输出中含有"我记得"但未调任何读取工具
    if "我记得" in (final_output or ""):
        has_read_tool = any(
            tc.get("name") in ("search_vault", "read_memory", "read_file", "read_folder")
            for tc in tool_calls
        )
        if not has_read_tool:
            codes.append(UNSUPPORTED_CLAIM)

    # 8. M 类：MEMORY_RECALL_MISS
    # 当 memory 类任务但没有调任何读工具
    if task_type == "memory":
        has_read = any(tc.get("name") in ("read_memory", "search_memories") for tc in tool_calls)
        if not has_read:
            codes.append(MEMORY_RECALL_MISS)

    # 去重
    seen = set()
    unique_codes: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique_codes.append(c)

    return unique_codes


def annotate_trace_with_failures(trace_dict: dict[str, Any]) -> dict[str, Any]:
    """对已序列化的 trace 注入 failure_codes。"""
    codes = detect_failure_codes(trace_dict)
    trace_dict["failure_codes"] = codes
    return trace_dict


# ===========================================================================
# 聚合分析
# ===========================================================================

def compute_failure_distribution(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """从一批 traces 中计算失败分布。"""
    from collections import Counter

    total = len(traces)
    failed_traces = [t for t in traces if not t.get("success") or t.get("failure_codes")]
    code_counts: Counter[str] = Counter()
    by_task_type: dict[str, Counter[str]] = {}
    code_trace_map: dict[str, list[dict]] = {}

    for t in traces:
        codes = t.get("failure_codes") or detect_failure_codes(t)
        if codes:
            for c in codes:
                code_counts[c] += 1
                code_trace_map.setdefault(c, []).append(t)

            tt = t.get("task_type", "unknown")
            if tt not in by_task_type:
                by_task_type[tt] = Counter()
            for c in codes:
                by_task_type[tt][c] += 1

    # 按类别聚合
    by_category: dict[str, int] = {}
    for code, cat in FAILURE_CATEGORIES.items():
        count = code_counts.get(code, 0)
        if count > 0:
            by_category[cat] = by_category.get(cat, 0) + count

    # 严重程度分布
    by_severity: dict[str, int] = {}
    for code, count in code_counts.items():
        sev = FAILURE_SEVERITY.get(code, "medium")
        by_severity[sev] = by_severity.get(sev, 0) + count

    return {
        "total_traces": total,
        "failed_traces": len(failed_traces),
        "failure_rate": round(len(failed_traces) / total * 100, 1) if total else 0,
        "by_code": dict(code_counts.most_common()),
        "by_category": by_category,
        "by_severity": by_severity,
        "by_task_type": {
            tt: dict(c.most_common())
            for tt, c in sorted(by_task_type.items())
        },
        # 每条 failure code 的 1 条代表 trace_id
        "representative_traces": {
            code: traces_list[0].get("trace_id", "?")
            for code, traces_list in code_trace_map.items()
        },
    }


def format_failure_report(dist: dict[str, Any]) -> str:
    """格式化输出失败分析报告。"""
    lines = [
        "\n" + "=" * 54,
        "  🔴 Failure Taxonomy Report",
        "=" * 54,
        f"  总 Trace: {dist['total_traces']}  |  失败: {dist['failed_traces']} "
        f"({dist['failure_rate']}%)",
        "=" * 54,
    ]

    # 按类别
    lines.append("\n  📂 按类别分布:")
    for cat, count in sorted(dist.get("by_category", {}).items(), key=lambda x: -x[1]):
        bar = "█" * min(20, count * 2)
        lines.append(f"    {cat:12s}  {bar}  {count}")

    # 按严重程度
    lines.append("\n  ⚠️  按严重程度分布:")
    for sev in ["critical", "high", "medium", "low"]:
        count = dist.get("by_severity", {}).get(sev, 0)
        if count > 0:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(sev, "•")
            lines.append(f"    {icon} {sev:8s}  {count}")

    # 按错误码
    lines.append(f"\n  🏷️  按错误码分布:")
    for code, count in dist.get("by_code", {}).items():
        desc = FAILURE_DESCRIPTIONS.get(code, "")
        sev = FAILURE_SEVERITY.get(code, "medium")
        pct = round(count / dist['total_traces'] * 100, 1) if dist['total_traces'] else 0
        trace_ref = dist.get("representative_traces", {}).get(code, "?")
        lines.append(f"    {code:35s}  {count:>3} ({pct:>5.1f}%)  [{sev}]")
        lines.append(f"      → {desc[:50]}")
        lines.append(f"      → 代表 Trace: {trace_ref}")

    # 按任务类型热力图
    if dist.get("by_task_type"):
        lines.append(f"\n  🔥 失败热力图 (按任务类型 × 错误码):")
        all_codes = sorted(set(
            c for tt_map in dist["by_task_type"].values() for c in tt_map
        ))
        if all_codes:
            header = f"    {'':16s}" + "".join(f"{c[:6]:>8s}" for c in all_codes)
            lines.append(header)
            for tt, code_map in sorted(dist["by_task_type"].items()):
                row = f"    {tt:16s}"
                for c in all_codes:
                    count = code_map.get(c, 0)
                    if count > 0:
                        row += f"    {count:1d}   "
                    else:
                        row += "    ·   "
                lines.append(row)

    lines.append("=" * 54 + "\n")
    return "\n".join(lines)
