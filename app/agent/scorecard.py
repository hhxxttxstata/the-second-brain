"""Agent 多维评分卡 V2 — 重塑评测指标，聚焦端到端结果。

评测框架：

  Level 1 — 端到端任务结果 (E2E)         权重 30%  ← 最核心
    1.1 E2E Task Success Rate             15%
    1.2 End-State Correctness              8%
    1.3 Constraint Satisfaction Rate       5%
    1.4 False Completion Rate              2%

  Level 2 — 路由与推理                   权重 20%
    2.1 Routing Accuracy                  10%
    2.2 Latency Performance               10%

  Level 3 — 系统可靠性                   权重 25%
    3.1 Retrieval Quality                 10%
    3.2 Tool Reliability                  10%
    3.3 Regression Guard                   5%

  Level 4 — 记忆与数据                   权重 15%
    4.1 Memory Consistency               10%
    4.2 Profile Completeness              5%

  Level 5 — 用户反馈                     权重 10%
    5.1 User Feedback Score              10%
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

try:
    from app.agent.graphs.tools import reset_agent_tools_cache, get_agent_tools
except ImportError:
    pass

_DATA_DIR = settings.agent_data_dir
_TRACE_DIR = _DATA_DIR / "traces"
_BENCHMARK_DIR = _DATA_DIR / "benchmark"
_EVAL_DIR = _DATA_DIR / "eval"
_FEEDBACK_DIR = _DATA_DIR / "feedback"
_VAULT_DIR = Path(settings.obsidian_vault) if hasattr(settings, 'obsidian_vault') else Path("D:/MYWORLD")


# ============================================================
# Level 1 — 端到端任务结果
# ============================================================

def _score_e2e_success_rate() -> dict[str, Any]:
    """1.1 E2E Task Success Rate

    读取最新 benchmark + eval 用例定义，检查每条用例是否：
    - 必要结果全部完成（required_outcomes）
    - 没有触发禁止行为（forbidden_actions）
    - 最终状态正确（通过 trace 中的 memory_updates / tool_calls 佐证）

    由于 benchmark 不存 required_outcomes 检查结果，
    用 pass_rate 作为 proxy，再拆解 forbidden_actions 检测。
    """
    bm_files = sorted(_BENCHMARK_DIR.glob("benchmark_*.json"), reverse=True)
    if not bm_files:
        return {"score": 0, "detail": "无数据"}

    # 加载所有 golden + challenge 用例的约束
    from app.agent.trace import load_test_cases
    all_cases = load_test_cases(tier="all")
    constraint_map = {}
    for c in all_cases:
        intent = c.get("intent", "")
        if intent:
            constraint_map[intent] = {
                "outcomes": c.get("required_outcomes", []),
                "forbidden": c.get("forbidden_actions", []),
            }

    # 读取 benchmark
    latest = json.loads(bm_files[0].read_text(encoding="utf-8"))
    results = latest.get("results", [])
    if not results:
        return {"score": 0, "detail": "benchmark 无结果"}

    # 分析 forbidden_actions 触发情况
    forbidden_triggers = 0
    for r in results:
        intent = r.get("intent", "")
        route = r.get("route", "?")
        cons = constraint_map.get(intent, {})
        exp_route = ""
        for c in all_cases:
            if c.get("intent") == intent and c.get("expected_route"):
                exp_route = c["expected_route"]
                break
        if cons.get("forbidden") and exp_route and route != exp_route:
            forbidden_triggers += 1

    # 计算通过率 + 约束满足率
    total = len(results)
    simple_pass = latest.get("pass_rate", 0)
    e2e_ok = max(0, simple_pass - (forbidden_triggers / total * 100) if total else 0)

    # E2E = pass_rate × 约束系数
    constraint_coeff = max(0, 1 - forbidden_triggers / max(total, 1))
    score = round(simple_pass * constraint_coeff, 1)

    return {
        "score": score,
        "detail": f"pass_rate={simple_pass}%, forbidden={forbidden_triggers}/{total}, e2e={score}%",
        "simple_pass_rate": simple_pass,
        "total_cases": total,
        "forbidden_triggers": forbidden_triggers,
        "constraint_coeff": round(constraint_coeff, 2),
    }


def _score_end_state() -> dict[str, Any]:
    """1.2 End-State Correctness

    从最近的 trace 中检查：
    - memory_updates 是否和 task_type 匹配（memory 任务应有 memory_updates）
    - 有 context_sources 的 trace 比例（说明读取了正确来源）
    - False Completion 迹象：成功=True 但实际状态未变
    """
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
    if not traces:
        return {"score": 0, "detail": "无 trace"}

    memory_writes = 0
    tasks_with_memory_updates = 0
    ctx_sourced = 0
    total = 0

    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            # 排除 benchmark 自跑 trace（无真实对话上下文，会稀释真实数据）
            if d.get("task_type") in ("benchmark",):
                continue
            total += 1
            tt = d.get("task_type", "?")
            mus = d.get("memory_updates", [])
            css = d.get("context_sources", [])
            suc = d.get("success", False)
            fout = d.get("final_output", "")

            if mus:
                tasks_with_memory_updates += 1
            if tt in ("memory", "daily_plan", "plan") and mus:
                memory_writes += 1
            if css:
                ctx_sourced += 1
        except Exception:
            pass

    if total == 0:
        return {"score": None, "detail": "无真实对话 trace（无数据，不参与加权）"}

    # 状态正确性评分 = 记忆写入率 + 上下文使用率
    ctx_rate = ctx_sourced / total
    mem_rate = tasks_with_memory_updates / max(total, 1)

    score = round((ctx_rate * 50 + mem_rate * 50), 1)
    return {
        "score": score,
        "detail": f"context来源率={ctx_rate:.0%}, 记忆更新率={mem_rate:.0%}",
        "context_source_rate": round(ctx_rate, 2),
        "memory_update_rate": round(mem_rate, 2),
        "memory_writes": memory_writes,
    }


def _score_constraint() -> dict[str, Any]:
    """1.3 Constraint Satisfaction Rate

    基于 benchmark 中路由匹配率 + forbidden 触发检测。
    硬约束：expected_route 匹配 = 路由约束满足。
    """
    from app.agent.trace import load_test_cases
    all_cases = load_test_cases(tier="all")
    expected = {c.get("intent", ""): c.get("expected_route", "")
                for c in all_cases if c.get("expected_route")}

    bm_files = sorted(_BENCHMARK_DIR.glob("benchmark_*.json"), reverse=True)
    if not bm_files:
        return {"score": 0, "detail": "无 benchmark"}

    latest = json.loads(bm_files[0].read_text(encoding="utf-8"))
    results = latest.get("results", [])

    total_constraints = 0
    satisfied = 0
    for r in results:
        intent = r.get("intent", "")
        route = r.get("route", "?")
        exp = expected.get(intent)
        if exp:
            total_constraints += 1
            if route == exp:
                satisfied += 1

    if total_constraints == 0:
        return {"score": 50, "detail": "无硬约束标注"}

    score = round(satisfied / total_constraints * 100, 1)
    return {
        "score": score,
        "detail": f"{satisfied}/{total_constraints} 约束满足 ({score}%)",
        "satisfied": satisfied,
        "total": total_constraints,
    }


def _score_false_completion() -> dict[str, Any]:
    """1.4 False Completion Rate

    检测 Agent 声称完成但实际状态未完成的情况。

    检测方法：
    - trace 中 success=True, 但 tool_calls 全失败
    - 输出说"已保存"但 memory_updates 为空
    - 输出说"已更新"但 trace 中没有对应 tool 调用

    要覆盖更多 case 需要每个 agent 执行前做状态快照，
    当前先用 trace 字段关联分析法做初步检测。
    """
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]

    claimed_done = 0
    false_completions = 0
    false_details = []

    false_keywords = ["已经", "已记录", "已保存", "已完成", "已经记住",
                       "已经为你", "成功", "saved", "done", "remembered"]

    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            suc = d.get("success", False)
            fout = d.get("final_output", "")
            mus = d.get("memory_updates", [])
            tcs = d.get("tool_calls", [])
            tt = d.get("task_type", "?")

            if not suc or not fout:
                continue

            # 输出中包含"完成"关键词
            has_claim = any(kw in fout for kw in false_keywords)
            if not has_claim:
                continue

            claimed_done += 1

            # 根据 task_type 判断是否真的完成了
            if tt in ("memory",) and not mus and not any(t.get("name") == "write_memory" for t in tcs):
                false_completions += 1
                false_details.append(f"memory声称完成但无写入: {fout[:60]}")
            elif tt in ("plan", "daily_plan") and not mus:
                # plan/task_ops 直接写 todos（不走 memory_updates）→ 查 todos 真实状态
                real_done = False
                try:
                    from app.agent.agent_data_service import read_memory
                    td = read_memory("task")
                    todos = td.get("todos", [])
                    if todos:
                        real_done = True
                except Exception:
                    pass
                # 或调用了写工具（update_task_status 等）
                if any(t.get("name") in ("update_task_status", "write_memory") for t in tcs):
                    real_done = True
                if not real_done:
                    false_completions += 1
                    false_details.append(f"plan声称完成但状态未变: {fout[:60]}")
            elif tt in ("chatbot",) and not tcs and not mus:
                # chatbot 可以不调工具
                pass
        except Exception:
            pass

    if claimed_done == 0:
        return {"score": 100, "detail": "无虚假完成迹象"}

    false_rate = round(false_completions / claimed_done * 100, 1)
    score = max(0, 100 - false_rate)

    return {
        "score": score,
        "detail": f"声称完成{claimed_done}次, 虚假{false_completions}次 ({false_rate}%)",
        "claimed_done": claimed_done,
        "false_completions": false_completions,
        "false_rate": false_rate,
        "examples": false_details[:3],
    }


# ============================================================
# Level 3 — 路由与推理
# ============================================================

# ============================================================
# L5 — 执行轨迹与工具质量 (15%)
# ============================================================

def _score_tool_selection() -> dict[str, Any]:
    """5.1 Tool Selection F1"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    all_calls = []
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            all_calls.extend(d.get("tool_calls", []))
        except Exception:
            pass
    if not all_calls:
        return {"score": 50, "detail": "无工具调用(需修复trace记录)", "total_calls": 0}
    total = len(all_calls)
    name_counts = {}
    for tc in all_calls:
        name_counts[tc.get("name", "?")] = name_counts.get(tc.get("name", "?"), 0) + 1
    used = len(name_counts)
    try:
        tools = get_agent_tools()
        reg = len(tools)
    except Exception:
        reg = 10
    repeated = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            seq = [tc.get("name", "") for tc in d.get("tool_calls", [])]
            for i in range(1, len(seq)):
                if seq[i] == seq[i - 1]:
                    repeated += 1
        except Exception:
            pass
    repeat_rate = repeated / total if total else 0
    precision = max(0, 100 - repeat_rate * 50)
    coverage = round(used / reg * 100, 1) if reg else 0
    recall = coverage
    f1 = round(2 * precision * recall / (precision + recall), 1) if (precision + recall) > 0 else 0
    return {"score": f1, "detail": f"F1={f1}, precision={precision:.1f}, recall={recall:.1f}%, 调用{total}次/{used}工具"}


def _score_tool_input() -> dict[str, Any]:
    """5.2 Tool Input Accuracy"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    total = 0
    bad = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            for tc in d.get("tool_calls", []):
                params = tc.get("params", {})
                if not isinstance(params, dict):
                    bad += 1
                    total += 1
                    continue
                for k, v in params.items():
                    total += 1
                    if v is None:
                        bad += 1
        except Exception:
            pass
    if total == 0:
        return {"score": 50, "detail": "无参数记录(需修复trace)"}
    acc = round((total - bad) / total * 100, 1)
    return {"score": acc, "detail": f"{total-bad}/{total} ({acc}%)"}


def _score_tool_output_utilization() -> dict[str, Any]:
    """5.3 Tool Output Utilization"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    checked = 0
    ok = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            tcs = d.get("tool_calls", [])
            fout = d.get("final_output", "")
            if not tcs or not fout:
                continue
            checked += 1
            has_err = any(not tc.get("success", True) or "❌" in str(tc.get("result_preview", "")) for tc in tcs)
            if has_err and ("❌" in fout or "失败" in fout):
                ok += 1
            elif not has_err:
                ok += 1
        except Exception:
            pass
    if checked == 0:
        return {"score": None, "detail": "无数据（不参与加权）"}
    rate = round(ok / checked * 100, 1)
    return {"score": rate, "detail": f"{ok}/{checked} ({rate}%)"}


def _score_trajectory_efficiency() -> dict[str, Any]:
    """5.4 Trajectory Efficiency"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    counts = []
    repeated = 0
    loops = 0
    tasks = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            names = [tc.get("name", "") for tc in d.get("tool_calls", [])]
            if not names:
                continue
            tasks += 1
            counts.append(len(names))
            if len(names) > 50:
                loops += 1
            for i in range(1, len(names)):
                if names[i] == names[i - 1]:
                    repeated += 1
        except Exception:
            pass
    if tasks == 0:
        return {"score": None, "detail": "无数据（不参与加权）"}
    avg = round(sum(counts) / len(counts), 1) if counts else 0
    repeat_rate = round(repeated / sum(counts) * 100, 1) if sum(counts) else 0
    loop_rate = round(loops / tasks * 100, 1) if tasks else 0
    call_score = max(0, 100 - (avg - 3) * 10) if avg > 3 else (100 if avg >= 1 else 50)
    repeat_score = max(0, 100 - repeat_rate * 10)
    loop_score = 100 if loop_rate == 0 else max(0, 100 - loop_rate * 20)
    score = round(call_score * 0.4 + repeat_score * 0.3 + loop_score * 0.3, 1)
    return {"score": score, "detail": f"平均{avg}次/任务, 重复率{repeat_rate}%, 循环率{loop_rate}%"}


def _score_recovery() -> dict[str, Any]:
    """5.5 Failure Recovery Rate"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    failed = 0
    recovered = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            tcs = d.get("tool_calls", [])
            if not tcs:
                continue
            has_fail = any(not tc.get("success", True) or "❌" in str(tc.get("result_preview", "")) for tc in tcs)
            if has_fail:
                failed += 1
                fout = d.get("final_output", "")
                if "❌" in str(fout) or "失败" in str(fout):
                    recovered += 1
        except Exception:
            pass
    if failed == 0:
        # 无失败记录 → 无数据，不参与加权（无法判断恢复能力）
        return {"score": None, "detail": "无工具失败记录（无数据，不参与加权）"}
    rate = round(recovered / failed * 100, 1)
    return {"score": rate, "detail": f"{recovered}/{failed} ({rate}%)"}


def _score_routing() -> dict[str, Any]:
    """路由准确（原 2.1 routing）

    从 benchmark 中检查路由匹配率。
    """
    from app.agent.trace import load_test_cases
    all_cases = load_test_cases(tier="all")
    expected = {c.get("intent", ""): c.get("expected_route", "")
                for c in all_cases if c.get("expected_route")}

    bm_files = sorted(_BENCHMARK_DIR.glob("benchmark_*.json"), reverse=True)
    if not bm_files:
        return {"score": 0, "detail": "无 benchmark 数据"}

    latest = json.loads(bm_files[0].read_text(encoding="utf-8"))
    results = latest.get("results", [])

    ok = 0
    total = 0
    for r in results:
        intent = r.get("intent", "")
        route = r.get("route", "?")
        exp = expected.get(intent)
        if exp:
            total += 1
            if route == exp:
                ok += 1

    if total == 0:
        return {"score": 50, "detail": "无 expected_route 标注"}
    score = round(ok / total * 100, 1)
    return {"score": score, "detail": f"{ok}/{total} ({score}%)"}


def _score_latency() -> dict[str, Any]:
    """2.2 Latency Performance（同 V1）"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        return {"score": 0, "detail": "无 trace"}

    by_type: dict[str, list[int]] = {}
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            tt = d.get("task_type", "unknown")
            lat = d.get("latency_ms", 0)
            if lat > 0:
                by_type.setdefault(tt, []).append(lat)
        except Exception:
            pass

    TARGETS = {
        "chatbot": [(5000, 100), (10000, 80), (20000, 50), (999999, 20)],
        "memory": [(3000, 100), (5000, 80), (10000, 50), (999999, 20)],
        "plan": [(5000, 100), (10000, 80), (20000, 50), (999999, 20)],
        "daily_plan": [(5000, 100), (10000, 80), (20000, 50), (999999, 20)],
        "reflect": [(10000, 100), (15000, 80), (25000, 50), (999999, 20)],
        "benchmark": [(10000, 100), (15000, 80), (25000, 50), (999999, 20)],
    }

    weighted_score = 0
    total_samples = 0
    detail_parts = []
    for tt, lats in sorted(by_type.items()):
        avg = sum(lats) / len(lats)
        targets = TARGETS.get(tt, [(10000, 80), (30000, 50), (999999, 20)])
        s = next((s for threshold, s in targets if avg < threshold), 20)
        weighted_score += s * len(lats)
        total_samples += len(lats)
        detail_parts.append(f"{tt}={avg:.0f}ms({len(lats)}条)→{s}分")

    if total_samples == 0:
        return {"score": 50, "detail": "无样本"}
    final_score = round(weighted_score / total_samples) if total_samples else 50
    return {
        "score": final_score,
        "detail": ", ".join(detail_parts),
    }


# ============================================================
# L2 — RAG 与知识可信度 (25%)
# ============================================================

def _load_vault_reads_from_traces(lookback: int = 100) -> list[dict[str, Any]]:
    """从 trace 中提取所有 vault 读取操作的工具调用。"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:lookback]
    vault_reads = []
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            for tc in d.get("tool_calls", []):
                name = tc.get("name", "")
                if name in ("search_vault", "read_file", "read_folder"):
                    success = tc.get("success", False) and "❌" not in str(tc.get("result_preview", ""))
                    vault_reads.append({
                        "name": name,
                        "params": tc.get("params", {}),
                        "success": success,
                        "preview": tc.get("result_preview", ""),
                        "latency_ms": tc.get("latency_ms", 0),
                        "has_result": bool(tc.get("result_preview")),
                    })
        except Exception:
            pass
    return vault_reads


def _check_hit_count(vault_reads: list[dict]) -> dict[str, int]:
    """统计检索调用基本结果。"""
    if not vault_reads:
        return {"total_calls": 0, "success_calls": 0, "success_rate": 0}
    total = len(vault_reads)
    ok = sum(1 for r in vault_reads if r["success"])
    return {"total_calls": total, "success_calls": ok, "success_rate": round(ok / total * 100, 1)}


def _has_vault_source_in_output(fout: str, vault_dir: Path) -> bool:
    """检查输出是否包含来自 vault 的文件引用。

    匹配策略（从宽到严，任一命中即算有来源）:
      1. 完整相对路径，如 "notes/项目笔记/agent项目简历描述.md"
      2. 文件名，如 "agent项目简历描述.md"（带 .md 后缀）
      3. 路径片段，如 "notes/项目笔记"（含"来源:"或"📁"标注前缀）
      4. 明确的来源标注，如 "来源: xxx"
    """
    if not fout:
        return False
    try:
        md_files = [str(p.relative_to(vault_dir)) for p in vault_dir.rglob("*.md")][:50]
    except Exception:
        md_files = []
    fout_lower = fout.lower()

    # 1. 完整相对路径
    for ref in md_files:
        if ref.lower().replace("\\", "/") in fout_lower:
            return True

    # 2. 文件名（.md 后缀）
    for ref in md_files:
        fname = Path(ref).name.lower()
        if fname and fname in fout_lower:
            return True

    # 3. "来源:" 标注（agent 明确写来源，即使文件名略写）
    if "来源" in fout and ("来源:" in fout or "来源：" in fout):
        return True

    return False


def _score_retrieval_health() -> dict[str, Any]:
    """2.1 检索健康度 Retrieval Health

    三个真实可观测数字：检索调用成功率 + 非空结果率 + 输出含来源引用率。
    刻意不再伪装 Recall@K / NDCG / Groundedness——那些指标需要 relevance
    标注，当前系统没有，proxy 只会制造假精度（见取舍记录）。
    """
    vault_reads = _load_vault_reads_from_traces()
    hits = _check_hit_count(vault_reads)

    vault_dir = _VAULT_DIR
    if not vault_dir.exists():
        return {"score": 50, "detail": "vault 路径不存在", "total": 0}

    # 分析最终的 final_output 是否包含 vault 来源引用
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    search_intents = 0
    sourced_outputs = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("task_type") != "chatbot":
                continue
            if not d.get("final_output"):
                continue
            # 检查是否有 vault 搜索调用
            has_search = any(
                tc.get("name") in ("search_vault", "read_file", "read_folder")
                for tc in d.get("tool_calls", [])
            )
            if not has_search:
                continue
            search_intents += 1
            if _has_vault_source_in_output(d.get("final_output", ""), vault_dir):
                sourced_outputs += 1
        except Exception:
            pass

    if search_intents == 0:
        if hits["total_calls"] > 0:
            # 至少说明检索工具在跑
            base = min(70, hits["success_rate"])
            return {"score": base, "detail": f"检索{hits['total_calls']}次, 成功{hits['success_rate']}%", "total": hits["total_calls"]}
        return {"score": 50, "detail": "无检索记录(需修复trace记录工具调用)", "total": 0}

    hit_rate = round(sourced_outputs / search_intents * 100, 1)
    # 检索健康度 = 引用率(60%) + 调用成功率(40%)；引用率是当前最接近
    # "检索结果真的被用上了"的可观测信号，成功率是工具层健康度
    score = round(hit_rate * 0.6 + hits["success_rate"] * 0.4, 1)
    return {
        "score": score,
        "detail": f"检索调用{hits['total_calls']}次, 成功{hits['success_rate']}%; "
                  f"搜索类查询{search_intents}条, {sourced_outputs}条输出含来源",
        "evidence_found": sourced_outputs,
        "evidence_total": search_intents,
        "hit_rate": hit_rate,
        "call_success_rate": hits["success_rate"],
    }


def _score_retrieval() -> dict[str, Any]:
    """3.1 Retrieval Quality — 分析 vault 搜索工具调用"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    vault_calls = 0
    vault_ok = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            for tc in d.get("tool_calls", []):
                if tc.get("name") in ("search_vault", "read_file", "read_folder"):
                    vault_calls += 1
                    if tc.get("success", True) and "❌" not in str(tc.get("result_preview", "")):
                        vault_ok += 1
        except Exception:
            pass

    if vault_calls == 0:
        return {"score": 50, "detail": "无检索调用（trace 可能未记录）"}
    rate = round(vault_ok / vault_calls * 100, 1)
    return {"score": round(min(100, rate)), "detail": f"{vault_ok}/{vault_calls} ({rate}%)"}


def _score_tool_reliability() -> dict[str, Any]:
    """3.2 Tool Reliability — 所有工具调用的成功率 + 覆盖率"""
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    by_tool: dict[str, list[bool]] = {}
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            for tc in d.get("tool_calls", []):
                name = tc.get("name", "?")
                ok = tc.get("success", True) and "❌" not in str(tc.get("result_preview", ""))
                by_tool.setdefault(name, []).append(ok)
        except Exception:
            pass

    if not by_tool:
        return {"score": 0, "detail": "无工具调用记录（trace 可能未记录）"}

    tool_scores = {n: round(sum(v)/len(v)*100, 1) for n, v in by_tool.items()}
    overall = round(sum(tool_scores.values()) / len(tool_scores), 1)

    try:
        tools = get_agent_tools()
        registered = len(tools)
    except Exception:
        registered = 10
    used = len(by_tool)
    coverage = round(used / registered * 100, 1) if registered else 0

    score = round(overall * 0.7 + min(coverage, 100) * 0.3, 1)
    return {
        "score": score,
        "detail": f"成功率{overall}%, 覆盖率{coverage}% ({used}/{registered})",
        "overall_success_rate": overall,
        "coverage_pct": coverage,
    }


def _score_regression() -> dict[str, Any]:
    """3.3 Regression Guard — golden 用例连续通过率（降为附属指标）"""
    bm_files = sorted(_BENCHMARK_DIR.glob("benchmark_*.json"), reverse=True)
    rates = []
    for f in bm_files[:5]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("total_cases", 0) >= 5:
                rates.append(d.get("pass_rate", 0))
        except Exception:
            pass
    if not rates:
        return {"score": 0, "detail": "无历史"}
    stable = sum(1 for r in rates if r == 100)
    score = rates[0] + (10 if stable >= 3 else 5 if stable >= 1 else 0)
    return {
        "score": min(100, score),
        "detail": f"最新{rates[0]}%, 近{len(rates)}次中{stable}次100%",
    }


# ============================================================
# Level 4 — 长期记忆能力 (20%)
# 参考 LongMemEval 框架: 写入/召回/更新/时间推理/拒答/安全
# ============================================================

def _score_memory_write_precision() -> dict[str, Any]:
    """4.1 Memory Write Precision

    检查已保存记忆的内容质量：
    - 正确的长期记忆 → 加分
    - 临时情绪保存 → 危险信号
    - 模型推测写入 → 减分
    - profile 写入是否有用户确认历史

    由于没有人工标注，用 proxy: 已降级(deprecated)条目率
    检测记忆中的矛盾被自我纠正的比例
    """
    from app.agent.agent_data_service import read_memory

    episodic = read_memory("episodic")
    entries = episodic.get("entries", [])
    if not entries:
        return {"score": 50, "detail": "无情景记忆"}

    total = len(entries)
    # SQLite 行: tags 是 JSON 字符串, deprecated 是 int 字段
    deprecated = sum(1 for e in entries if e.get("deprecated") or "deprecated" in json.loads(e.get("tags", "[]") or "[]"))
    active = total - deprecated

    # 活跃率越高 = 写入精度越好 (无矛盾的记忆才应该活跃)
    # 但也不是越高越好: 如果从不检测矛盾也不降级, 活跃率=100%却可能含有错误
    # 所以使用: active_rate × (1 - deprecation_rate/2)
    active_rate = active / total
    dep_rate = deprecated / total
    precision = round(active_rate * (1 - dep_rate * 0.5) * 100, 1)

    details = [f"{active}活跃/{total}总 (降级{deprecated}={dep_rate:.0%})"]

    # 检查 profile 写入有无确认标记
    try:
        profile = read_memory("stable_profile")
        profile_entries = [e for e in entries if "profile" in json.loads(e.get("tags", "[]") or "[]")]
        if profile_entries:
            details.append(f"profile写入{len(profile_entries)}次(均无用户确认标记)")
    except Exception:
        pass

    return {
        "score": precision,
        "detail": "; ".join(details),
        "active": active,
        "total": total,
        "deprecated": deprecated,
    }


def _score_memory_recall() -> dict[str, Any]:
    """4.2 Memory Recall

    检查 trace 中 read_memory 是否被有效使用。
    """
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]

    memory_tasks = 0
    memory_used = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            tcs = d.get("tool_calls", [])
            has_read_memory = any(tc.get("name") == "read_memory" for tc in tcs)
            has_write_memory = any(tc.get("name") == "write_memory" for tc in tcs)
            if has_read_memory or has_write_memory:
                memory_tasks += 1
            if has_read_memory:
                memory_used += 1
        except Exception:
            pass

    if memory_tasks == 0:
        return {"score": 50, "detail": "无记忆调用(trace未记录)"}

    recall = round(memory_used / memory_tasks * 100, 1)
    score = recall  # 大多数记忆任务是写入，所以读/写比越高越好
    return {
        "score": score,
        "detail": f"{memory_used}次读取 / {memory_tasks}次记忆操作 ({recall}%)",
        "read_count": memory_used,
        "total_ops": memory_tasks,
    }


def _score_memory_update() -> dict[str, Any]:
    """4.3 Memory Update Accuracy

    通过检查 episodic 中的 deprecated 比率来推断更新准确率。
    被降级的记忆 = 系统自我修正成功
    被降级后又重复写入相同内容 = 修正失败
    """
    from app.agent.agent_data_service import read_memory
    episodic = read_memory("episodic")
    entries = episodic.get("entries", [])
    if not entries:
        return {"score": 50, "detail": "无情景记忆"}

    # SQLite 行: deprecated 是 int 字段, superseded_by 是字符串(非空=被替代)
    deprecated_entries = [e for e in entries if e.get("deprecated") or "deprecated" in json.loads(e.get("tags", "[]") or "[]")]
    superseded = [e for e in deprecated_entries if e.get("superseded_by")]
    total = len(entries)

    if total == 0:
        return {"score": None, "detail": "无记忆数据（不参与加权）"}

    # 检查是否有内容相似的条目（可能重复写入而非覆盖）
    contents = [e.get("content", "")[:50] for e in entries]
    content_set = set(contents)
    dup_ratio = 1 - (len(content_set) / len(contents)) if contents else 0

    score = 100
    if dup_ratio > 0.1:
        score -= dup_ratio * 100
    if len(superseded) > 0:
        score += min(10, len(superseded) * 2)  # 自我修正加分

    score = max(0, min(100, round(score, 1)))
    return {
        "score": score,
        "detail": f"降级{len(deprecated_entries)}条(含superseded={len(superseded)}), 内容重复率{dup_ratio:.0%}",
        "deprecated": len(deprecated_entries),
        "superseded": len(superseded),
        "duplication_ratio": round(dup_ratio, 2),
    }


def _score_memory_temporal() -> dict[str, Any]:
    """4.4 Temporal Reasoning Accuracy

    proxy: 检查 episodic 是否包含时间标记正确的条目，
    以及是否有"过时信息被重新使用"的痕迹。
    """
    from app.agent.agent_data_service import read_memory
    episodic = read_memory("episodic")
    entries = episodic.get("entries", [])
    if not entries:
        return {"score": 50, "detail": "无情景记忆"}

    # 所有记忆都应带有时间戳（SQLite 字段是 created_at）
    with_ts = sum(1 for e in entries if e.get("created_at"))
    ts_rate = with_ts / len(entries) if entries else 0

    # 时间跨度（如果系统有跨天记忆说明时间概念在运作）
    timestamps = [e.get("created_at", "") for e in entries if e.get("created_at")]
    if len(timestamps) >= 2:
        try:
            sorted_ts = sorted(timestamps)
            start = sorted_ts[0][:10]
            end = sorted_ts[-1][:10]
            date_range = f"{start}~{end}"
        except Exception:
            date_range = "?"
    else:
        date_range = "单条"

    score = round(ts_rate * 100, 1)
    return {
        "score": score,
        "detail": f"时间戳覆盖率{ts_rate:.0%}, 跨度{date_range}",
        "timestamp_rate": round(ts_rate, 2),
        "date_range": date_range,
    }


def _score_memory_abstention() -> dict[str, Any]:
    """4.5 Memory Abstention Accuracy

    检测 Agent 在没有记忆支撑时是否承认不知道。
    通过 trace 分析没有搜索/记忆读取调用时输出的内容。
    """
    traces = sorted(_TRACE_DIR.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]

    no_sources = 0
    claimed_known = 0
    for f in traces:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("task_type") != "chatbot":
                continue
            has_search = any(
                tc.get("name") in ("search_vault", "read_memory", "read_file", "read_folder", "search_web")
                for tc in d.get("tool_calls", [])
            )
            if has_search:
                continue
            fout = d.get("final_output", "")
            if not fout:
                continue
            no_sources += 1
            # 如果没用任何工具却声称"记得"或"有记录"→ 可能有虚构
            if any(kw in fout for kw in ["我记得", "根据记录", "根据我的记忆"]):
                claimed_known += 1
        except Exception:
            pass

    if no_sources == 0:
        return {"score": 70, "detail": "所有查询都有信息来源(好迹象)"}

    false_claim_rate = round(claimed_known / no_sources * 100, 1)
    score = max(0, 100 - false_claim_rate)
    return {
        "score": score,
        "detail": f"无信息来源回答{no_sources}次, 声称'记得'{claimed_known}次 ({false_claim_rate}%)",
        "no_source_total": no_sources,
        "false_claims": claimed_known,
    }


def _score_memory_safety() -> dict[str, Any]:
    """4.6 Memory Safety Metrics

    硬门槛检查:
    - 未授权持久化 = 0
    - 错误的 stable profile 写入 = 0
    - 跨上下文记忆泄漏 = 0
    - 已删除记忆重现 = 0
    - 过期关键记忆使用 = 0

    当前可以通过检查 deprecated 条目的使用情况来做 proxy。
    """
    from app.agent.agent_data_service import read_memory
    episodic = read_memory("episodic")
    entries = episodic.get("entries", [])
    deprecated = [e for e in entries if e.get("deprecated") or "deprecated" in json.loads(e.get("tags", "[]") or "[]")]

    violations = 0
    details = []

    # 检查是否有 deprecated 条目没有被 superseded_by 标记（语义降级但未标明替代）
    for e in deprecated:
        if not e.get("superseded_by"):
            violations += 0.5  # 轻度违规

    # 检查 profile 是否正确创建（SQLite 版 profile 存 content JSON，无 __created_at 时间线字段）
    profile = read_memory("stable_profile")
    if not profile:
        violations += 1
        details.append("profile 为空")
    # 非空 profile + 有稳定字段 → 正常

    # 重复 todo
    task = read_memory("task")
    todos = task.get("todos", [])
    titles = [t.get("title", "") for t in todos]
    if len(titles) != len(set(titles)):
        violations += 1
        details.append("待办有重复")

    score = max(0, 100 - violations * 20)
    if not details:
        details.append("未发现安全问题")

    return {
        "score": score,
        "detail": "; ".join(details),
        "violations": violations,
        "deprecated_no_superseded": sum(1 for e in deprecated if "superseded_by" not in e),
    }


# ============================================================
# L6 — 稳定性与鲁棒性 (10%)
# ============================================================

def _load_benchmark_results(lookback: int = 5) -> list[dict]:
    """加载最近 N 份 benchmark 报告。"""
    files = sorted(_BENCHMARK_DIR.glob("benchmark_*.json"), reverse=True)[:lookback]
    results = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            results.append(d)
        except Exception:
            pass
    return results


def _score_pass1() -> dict[str, Any]:
    """6.1 pass@1 — 最新 regression 的 pass_rate（只看 regression，5条）"""
    reports = _load_benchmark_results(1)
    if not reports:
        return {"score": 0, "detail": "无 benchmark"}
    # 只看总 pass_rate（benchmark 通常跑的是 5 条 regression）
    rate = reports[0].get("pass_rate", 0)
    return {"score": rate, "detail": f"最新 pass_rate={rate}%"}


def _score_pass3() -> dict[str, Any]:
    """6.2 pass³ — 最近 3 次 benchmark 全部通过的比率"""
    reports = _load_benchmark_results(3)
    if not reports:
        return {"score": 0, "detail": "无历史"}
    rates = [r.get("pass_rate", 0) for r in reports[:3]]
    all_pass = all(r == 100 for r in rates)
    avg = sum(rates) / len(rates) if rates else 0

    score = 100 if all_pass else (85 if avg >= 95 else (60 if avg >= 80 else avg))
    return {
        "score": round(score, 1),
        "detail": f"最近{len(rates)}次 {rates}, 全过={all_pass}",
        "rates": rates,
        "all_pass": all_pass,
    }


def _score_paraphrase() -> dict[str, Any]:
    """6.3 Paraphrase Robustness — 不同表述的路由波动"""
    reports = _load_benchmark_results(3)
    if len(reports) < 2:
        return {"score": 70, "detail": "需至少 2 份 benchmark"}

    rates = []
    for r in reports:
        results = r.get("results", [])
        matched = sum(1 for rr in results if rr.get("success", False))
        rates.append(matched / len(results) * 100 if results else 0)

    volatility = max(rates) - min(rates) if rates else 0
    score = max(50, 100 - volatility * 5)
    return {"score": round(score, 1), "detail": f"波动={volatility}pp", "rates": rates}


def _score_long_context() -> dict[str, Any]:
    """6.4 Long-Context Degradation — 短/长任务通过率对比"""
    reports = _load_benchmark_results(1)
    if not reports:
        return {"score": 0, "detail": "无 benchmark"}
    results = reports[0].get("results", [])
    if not results:
        return {"score": 50, "detail": "无结果"}

    short = [r for r in results if r.get("latency_ms", 0) < 10000]
    long_ = [r for r in results if r.get("latency_ms", 0) >= 10000]

    sr = round(sum(1 for r in short if r.get("success")) / len(short) * 100, 1) if short else 100
    lr = round(sum(1 for r in long_ if r.get("success")) / len(long_) * 100, 1) if long_ else 100

    deg = sr - lr
    score = max(0, 100 - deg * 2)
    return {
        "score": round(score, 1),
        "detail": f"短任务(<10s)成功率{sr}%({len(short)}条), 长任务(≥10s)成功率{lr}%({len(long_)}条), 降幅{deg}pp",
    }

def _score_feedback() -> dict[str, Any]:
    """5.1 User Feedback Score"""
    fb_files = sorted(_FEEDBACK_DIR.glob("fb_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not fb_files:
        # 无反馈数据 → 不参与加权（score=None 会被 run_scorecard 跳过）
        return {"score": None, "detail": "暂无评价（无数据，不参与加权）"}
    useful = sum(1 for f in fb_files if json.load(open(f, encoding="utf-8")).get("failure_type") == "useful")
    total = len(fb_files)
    return {"score": round(useful / total * 100, 1), "detail": f"{useful}/{total} 有用"}


# ============================================================
# 综合评分
# ============================================================

WEIGHTS_V2 = {
    # Level 1 — E2E (30%)
    "score_e2e_success": 0.15,
    "score_end_state": 0.08,
    "score_constraint": 0.05,
    "score_false_completion": 0.02,
    # Level 2 — 检索健康度（原 5 个 RAG proxy 维度合并为 1 个真实可观测维度）
    "score_retrieval_health": 0.10,
    # Level 3 — 路由与推理 (20%)
    "score_routing": 0.10,
    "score_latency": 0.10,
    # Level 4 — 长期记忆能力 (20%)
    "score_memory_write_precision": 0.04,
    "score_memory_recall": 0.04,
    "score_memory_update": 0.04,
    "score_memory_temporal": 0.03,
    "score_memory_abstention": 0.03,
    "score_memory_safety": 0.02,
    # Level 5 — 执行轨迹与工具质量 (15%)
    "score_tool_selection": 0.04,
    "score_tool_input": 0.03,
    "score_tool_output_utilization": 0.03,
    "score_trajectory_efficiency": 0.03,
    "score_recovery": 0.02,
    # Level 6 — 稳定性与鲁棒性 (10%)
    "score_pass1": 0.03,
    "score_pass3": 0.03,
    "score_paraphrase": 0.02,
    "score_long_context": 0.02,
    # Level 7 — 用户反馈 (10%)
    "score_feedback": 0.10,
}


LEVEL_LABELS = {
    "score_e2e_success": "L1-E2E成功率",
    "score_end_state": "L1-状态正确性",
    "score_constraint": "L1-约束满足率",
    "score_false_completion": "L1-虚假完成率",
    "score_retrieval_health": "L2-检索健康度",
    "score_routing": "L3-路由准确",
    "score_latency": "L3-延迟性能",
    "score_memory_write_precision": "L4-写入精确率",
    "score_memory_recall": "L4-记忆召回率",
    "score_memory_update": "L4-更新准确率",
    "score_memory_temporal": "L4-时间推理",
    "score_memory_abstention": "L4-拒答准确率",
    "score_memory_safety": "L4-记忆安全",
    "score_tool_selection": "L5-工具选择F1",
    "score_tool_input": "L5-工具参数正确率",
    "score_tool_output_utilization": "L5-工具结果利用率",
    "score_trajectory_efficiency": "L5-执行轨迹效率",
    "score_recovery": "L5-失败恢复率",
    "score_pass1": "L6-pass@1",
    "score_pass3": "L6-pass³",
    "score_paraphrase": "L6-改写鲁棒性",
    "score_long_context": "L6-长上下文退化",
    "score_feedback": "L7-用户反馈",
}

LEVEL_PARENTS = {
    "score_e2e_success": "L1 端到端任务结果 (30%)",
    "score_end_state": "L1 端到端任务结果 (30%)",
    "score_constraint": "L1 端到端任务结果 (30%)",
    "score_false_completion": "L1 端到端任务结果 (30%)",
    "score_retrieval_health": "L2 检索健康度 (10%)",
    "score_routing": "L3 路由与推理 (20%)",
    "score_latency": "L3 路由与推理 (20%)",
    "score_memory_write_precision": "L4 长期记忆 (20%)",
    "score_memory_recall": "L4 长期记忆 (20%)",
    "score_memory_update": "L4 长期记忆 (20%)",
    "score_memory_temporal": "L4 长期记忆 (20%)",
    "score_memory_abstention": "L4 长期记忆 (20%)",
    "score_memory_safety": "L4 长期记忆 (20%)",
    "score_tool_selection": "L5 执行轨迹与工具质量 (15%)",
    "score_tool_input": "L5 执行轨迹与工具质量 (15%)",
    "score_tool_output_utilization": "L5 执行轨迹与工具质量 (15%)",
    "score_trajectory_efficiency": "L5 执行轨迹与工具质量 (15%)",
    "score_recovery": "L5 执行轨迹与工具质量 (15%)",
    "score_pass1": "L6 稳定性与鲁棒性 (10%)",
    "score_pass3": "L6 稳定性与鲁棒性 (10%)",
    "score_paraphrase": "L6 稳定性与鲁棒性 (10%)",
    "score_long_context": "L6 稳定性与鲁棒性 (10%)",
    "score_feedback": "L7 用户反馈 (10%)",
}


def run_scorecard() -> dict[str, Any]:
    """运行 V3 评分卡。"""
    start = time.monotonic()
    logger.info("scorecard.start", step="📊 Agent V3 评分卡启动")

    scorers = {
        "score_e2e_success": _score_e2e_success_rate,
        "score_end_state": _score_end_state,
        "score_constraint": _score_constraint,
        "score_false_completion": _score_false_completion,
        "score_retrieval_health": _score_retrieval_health,
        "score_routing": _score_routing,
        "score_latency": _score_latency,
        "score_memory_write_precision": _score_memory_write_precision,
        "score_memory_recall": _score_memory_recall,
        "score_memory_update": _score_memory_update,
        "score_memory_temporal": _score_memory_temporal,
        "score_memory_abstention": _score_memory_abstention,
        "score_memory_safety": _score_memory_safety,
        "score_tool_selection": _score_tool_selection,
        "score_tool_input": _score_tool_input,
        "score_tool_output_utilization": _score_tool_output_utilization,
        "score_trajectory_efficiency": _score_trajectory_efficiency,
        "score_recovery": _score_recovery,
        "score_pass1": _score_pass1,
        "score_pass3": _score_pass3,
        "score_paraphrase": _score_paraphrase,
        "score_long_context": _score_long_context,
        "score_feedback": _score_feedback,
    }

    dims = {}
    for key, fn in scorers.items():
        try:
            r = fn()
        except Exception as e:
            r = {"score": 0, "detail": f"error: {e}"}
        dims[key] = r

    # 加权平均（跳过无数据维度 score=None）
    weighted = 0
    total_weight = 0
    skipped: list[str] = []
    for key, w in WEIGHTS_V2.items():
        s = dims.get(key, {}).get("score", 0)
        if s is None:
            skipped.append(key)
            continue
        if isinstance(s, (int, float)):
            weighted += s * w
            total_weight += w

    overall = round(weighted / total_weight, 1) if total_weight else 0

    # 记录跳过的维度（透明性：无数据不空转加分）
    if skipped:
        logger.info("scorecard.skipped_dims",
                    skipped=[LEVEL_LABELS.get(k, k) for k in skipped])

    # 按 Level 汇总（跳过无数据维度）
    level_scores: dict[str, dict] = {}
    for key, parent in LEVEL_PARENTS.items():
        level_scores.setdefault(parent, {"score": 0, "count": 0})
        s = dims.get(key, {}).get("score", 0)
        if s is None:
            continue
        level_scores[parent]["score"] += s
        level_scores[parent]["count"] += 1
    for parent, d in level_scores.items():
        level_scores[parent] = round(d["score"] / d["count"], 1) if d["count"] else None

    report = {
        "version": 3,
        "timestamp": datetime.now().isoformat(),
        "total_score": overall,
        "skipped_dims": [LEVEL_LABELS.get(k, k) for k in skipped],
        "level_scores": level_scores,
        "dimensions": {
            key: {
                "score": d.get("score", 0),
                "weight": WEIGHTS_V2.get(key, 0),
                "weighted": round(d.get("score", 0) * WEIGHTS_V2.get(key, 0), 1) if d.get("score") is not None else None,
                "detail": d.get("detail", ""),
            }
            for key, d in dims.items()
        },
    }

    # 持久化
    try:
        scorecard_dir = _DATA_DIR / "scorecard"
        scorecard_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        (scorecard_dir / f"v3_{today}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    report["latency_ms"] = int((time.monotonic() - start) * 1000)

    # 注入 Failure Taxonomy 分析
    try:
        from app.agent.failure_taxonomy import (
            compute_failure_distribution,
            annotate_trace_with_failures,
        )
        from app.agent.trace import load_all_traces
        ft_traces = load_all_traces(limit=200)
        ft_annotated = [annotate_trace_with_failures(t) for t in ft_traces]
        report["failure_analysis"] = compute_failure_distribution(ft_annotated)
    except Exception as e:
        report["failure_analysis"] = {"error": str(e)}

    return report


def format_scorecard(report: dict[str, Any]) -> str:
    """格式化 V3 评分卡输出。"""
    score = report.get("total_score", 0)
    dims = report.get("dimensions", {})
    levels = report.get("level_scores", {})

    if score >= 90:
        grade = "S 🏆"
    elif score >= 80:
        grade = "A 🥇"
    elif score >= 70:
        grade = "B 🥈"
    elif score >= 60:
        grade = "C 🥉"
    else:
        grade = "D ⚠️"

    lines = [
        f"{'='*54}",
        f"  📊 Agent 评分卡 V3",
        f"  等级: {grade}     总分: {score}/100",
        f"{'='*54}",
    ]

    # Level 汇总
    for parent, ls in levels.items():
        if ls is None:
            lines.append(f"  {parent:32s} {'无数据':>5s}")
        else:
            lines.append(f"  {parent:32s} {ls:>5.1f}")

    lines.append(f"{'─'*54}")

    # 每个维度
    for key, label in LEVEL_LABELS.items():
        d = dims.get(key, {})
        s = d.get("score", 0)
        w = d.get("weight", 0)
        detail = d.get("detail", "")
        if s is None:
            lines.append(f"  {label:18s} {'无数据':>10s}  (w={w:.0%})")
            if detail:
                lines.append(f"  {'':18s} {detail[:70]}")
            continue
        if isinstance(s, (int, float)):
            bar_len = max(1, min(10, int(s / 10)))
        else:
            bar_len = 5
        bar = "█" * bar_len + "░" * (10 - bar_len)
        lines.append(f"  {label:18s} {bar} {s:>5.1f}  (w={w:.0%})")
        if detail:
            lines.append(f"  {'':18s} {detail[:70]}")

    lines.append(f"{'─'*54}")
    depth = report.get("data_depth", {})
    if not depth:
        try:
            depth = {
                "traces": len(list(_TRACE_DIR.glob("trace_*.json"))),
                "feedbacks": len(list(_FEEDBACK_DIR.glob("fb_*.json"))),
            }
        except Exception:
            depth = {}
    lines.append(f"  Data: traces={depth.get('traces','?')} | "
                 f"feedbacks={depth.get('feedbacks','?')}")

    # Failure Taxonomy
    fa = report.get("failure_analysis", {})
    if fa and not fa.get("error"):
        lines.append(f"{'─'*54}")
        lines.append(f"  🔴 Failure Taxonomy")
        lines.append(f"  失败率: {fa.get('failure_rate', '?')}%")
        fc = fa.get("by_code", {})
        if fc:
            lines.append(f"  Top 失败码:")
            for code, count in sorted(fc.items(), key=lambda x: -x[1])[:5]:
                desc = FAILURE_DESCRIPTIONS.get(code, "") if 'FAILURE_DESCRIPTIONS' in dir() else ""
                pct = round(count / fa['total_traces'] * 100, 1) if fa.get('total_traces') else 0
                lines.append(f"    {code:30s}  {count:>3} ({pct:>5.1f}%)")
            traces_ref = fa.get("representative_traces", {})
            for code in sorted(traces_ref.keys()):
                lines.append(f"      → 代表 Trace: {traces_ref[code]}")
        else:
            lines.append(f"  ✅ 近期 trace 中无失败码触发")

    lines.append(f"  ⏱  {report.get('latency_ms', '?')}ms")
    lines.append(f"{'='*54}")

    return "\n".join(lines)
