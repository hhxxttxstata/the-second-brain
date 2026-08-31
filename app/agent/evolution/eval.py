"""Evolution Eval — 自进化评测：演化前后对比 suite（Evaluation Lifecycle §P2-2）。

回答"自进化是否真的让 agent 更快更熟练成本更低"：
  阶段 A（基线）：隔离环境跑任务集 → 指标
  阶段 B（演化后）：注入预设策略/skill（模拟已固化的自进化产物）→ 同任务集 → 指标
  对比：成功率（硬门槛：B 不 < A）+ latency/tokens/tool_calls 变化

评测输入：agent_data/eval/evolution/*.json（简单 case 列表，仅 input + 可选 fixture）
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

from ..trace import isolate_agent_data
from . import update as evo_update
from . import experience as exp

_EVOL_DIR = settings.agent_data_dir / "eval" / "evolution"
_REPORT_DIR = settings.agent_data_dir / "benchmark"

# 硬门槛：演化后成功率下降超过该值 → FAIL
SUCCESS_DROP_HARD_LIMIT = 0.0          # 不允许下降（0 容忍）
LATENCY_IMPROVE_TARGET_PCT = -5        # 期望 latency 改善 ≥5%
TOKENS_IMPROVE_TARGET_PCT = -5         # 期望 token 改善 ≥5%


def load_evolution_cases() -> list[dict[str, Any]]:
    """读取演化评测任务集（agent_data/eval/evolution/*.json）。"""
    if not _EVOL_DIR.exists():
        return []
    cases: list[dict[str, Any]] = []
    for f in sorted(_EVOL_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            arr = data if isinstance(data, list) else [data]
            cases.extend(arr)
        except (json.JSONDecodeError, OSError):
            continue
    return cases


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    """跑单个演化 case，返回 latency/tokens/success/工具轨迹。"""
    from ..graphs.orchestrator import run_orchestrator
    from ..trace import _TRACE_DIR

    start = time.monotonic()
    r = run_orchestrator(input_text=case.get("input", ""))
    latency_ms = int((time.monotonic() - start) * 1000)
    # token：读 orchestrator 内部 trace（隔离模式下 _TRACE_DIR 指向临时目录）
    tokens = 0
    tool_names: list[str] = []
    try:
        t = r.get("trace_id", "")
        if t:
            p = _TRACE_DIR / f"{t}.json"
            if p.exists():
                inner = json.loads(p.read_text(encoding="utf-8"))
                tokens = int(inner.get("total_tokens") or 0)
                tool_names = [str(tc.get("name", "")) for tc in (inner.get("tool_calls") or [])]
    except Exception:
        pass
    return {
        "case_id": case.get("id", case.get("input", "?")[:20]),
        "input": case.get("input", ""),
        "success": bool(r.get("success", False)),
        "route": r.get("route", "?"),
        "latency_ms": latency_ms,
        "tokens": tokens,
        "tools": tool_names,
    }


def _aggregate_evol(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"count": 0}
    lat = [r["latency_ms"] for r in results]
    return {
        "count": len(results),
        "success_rate": round(sum(1 for r in results if r["success"]) / len(results) * 100, 1),
        "avg_latency_ms": round(sum(lat) / len(lat)) if lat else 0,
        "total_tokens": sum(r["tokens"] for r in results),
    }


def run_evolution_suite(cases: list[dict[str, Any]] | None = None,
                        dry_run: bool = False) -> dict[str, Any]:
    """演化前后对比评测。

    阶段 B 注入一组预设策略（写入隔离环境的 policies），模拟"自进化已沉淀"的状态。
    """
    if cases is None:
        cases = load_evolution_cases()
    if not cases:
        return {"success": False, "error": "agent_data/eval/evolution/ 无评测任务",
                "hint": "参考 docs/handoff-eval-lifecycle.md §P2-2 创建 case"}

    # 预设策略：模拟自进化已固化（对所有 case 的 task_type 生效）
    PRESET_POLICIES = [
        {
            "task_type": "chatbot",
            "trigger": "评测任务",
            "action": "直接使用最合适的工具完成任务，避免多余检索；回答前先确认关键状态。",
            "benefit": "减少不必要工具调用",
        },
        {
            "task_type": "memory",
            "trigger": "评测任务",
            "action": "一次对话内的多条记忆合并为一次批量写入。",
            "benefit": "减少工具调用",
        },
        {
            "task_type": "chatbot",
            "trigger": "用户要求新工具/写工具",
            "action": "按 create_tool 规范：先与用户确认意图，再调用 create_tool 创建并验证。",
            "benefit": "L3 自举：缺工具时自写工具",
        },
    ]

    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "type": "evolution_suite",
        "case_count": len(cases),
    }

    if dry_run:
        report["dry_run"] = True
        report["cases"] = [{"case_id": c.get("id", c.get("input", "?")[:20]),
                            "input": c.get("input", "")} for c in cases]
        return report

    with isolate_agent_data() as tmp:
        # ── 阶段 A：基线 ──
        logger.info("evolve.eval.phase_a", step="📊 阶段 A：基线（干净环境）...")
        phase_a = [_run_case(c) for c in cases]

        # ── 阶段 B：注入策略（模拟自进化沉淀）──
        logger.info("evolve.eval.phase_b", step="🧬 阶段 B：注入策略后...")
        evo_update.apply_policy_suggestions(PRESET_POLICIES)
        # 验证注入块确实可构建（策略真的进入了上下文管线）
        block = evo_update.build_evolution_block(task="评测任务")
        phase_b = [_run_case(c) for c in cases]

    agg_a, agg_b = _aggregate_evol(phase_a), _aggregate_evol(phase_b)
    report["phase_a"] = agg_a
    report["phase_b"] = agg_b
    report["policy_injected"] = bool(block and "执行策略" in block)

    # ── L3 自举观测：create_tool 是否被触发（Evaluation Lifecycle §7.3）──
    tools_a = sorted({t for r in phase_a for t in r.get("tools", [])})
    tools_b = sorted({t for r in phase_b for t in r.get("tools", [])})
    report["tools_phase_a"] = tools_a
    report["tools_phase_b"] = tools_b
    report["create_tool_triggered"] = "create_tool" in tools_b or "create_tool" in tools_a

    # ── 对比判定 ──
    hard_pass = True
    notes: list[str] = []
    if agg_a["count"] > 0:
        delta_success = agg_b["success_rate"] - agg_a["success_rate"]
        report["success_delta_pct"] = round(delta_success, 1)
        if delta_success < SUCCESS_DROP_HARD_LIMIT:
            hard_pass = False
            notes.append(f"成功率下降 {delta_success}%（硬门槛）")
        else:
            notes.append(f"成功率 {agg_a['success_rate']}% → {agg_b['success_rate']}%")

        if agg_a["avg_latency_ms"] > 0 and agg_b["avg_latency_ms"] > 0:
            lat_delta = round((agg_b["avg_latency_ms"] - agg_a["avg_latency_ms"])
                              / agg_a["avg_latency_ms"] * 100, 1)
            report["latency_delta_pct"] = lat_delta
            notes.append(f"latency {agg_a['avg_latency_ms']}ms → {agg_b['avg_latency_ms']}ms "
                         f"({lat_delta:+.1f}%)")

        report["hard_gate"] = "PASS" if hard_pass else "FAIL"
        report["notes"] = notes

    report["results_a"] = phase_a
    report["results_b"] = phase_b

    # 报告落盘（真实 benchmark 目录，历史可查）
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        safe = datetime.now().isoformat()[:10].replace("-", "_")
        (_REPORT_DIR / f"evolution_{safe}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return report
