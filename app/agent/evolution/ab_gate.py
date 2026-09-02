"""AB Gate — 策略晋升门（P2）：策略固化 skill 前的受控 A/B 评测。

动机：L2 的 runtime 窗口对比（最近 5 vs 之前 5）样本小、噪声大，单票晋升统计效度不足；
固化是"格式升级"（policy 一行 → 常驻 skill 文件），值得一次隔离实验再放行。

流程（全部在 isolate_agent_data 隔离区内，不污染生产状态）：
  阶段 A：干净基线（无任何自进化产物）跑 route 匹配的小任务集
  阶段 B：在隔离区模拟固化（_promote_to_skill 写隔离 skills/）→ 同任务集
  判定：成功率不降（硬门，与 evolution suite 的 SUCCESS_DROP_HARD_LIMIT 一致）
        + latency/tokens delta 记录在报告里

LLM 不可用（任一阶段有用例异常）→ pass=None（unavailable），调用方降级为 promote_pending。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

from ..trace import isolate_agent_data
from . import eval as evo_eval
from . import meta

_REPORT_DIR = settings.agent_data_dir / "benchmark"

# 硬门槛：阶段 B 成功率相对 A 不允许下降（0 容忍，同 evolution suite）
SUCCESS_DROP_HARD_LIMIT = 0.0


def select_cases(policy: dict[str, Any], max_cases: int = 6) -> list[dict[str, Any]]:
    """选 A/B 用例：evolution suite 中 route 匹配的 case，不足补 golden 同 route。"""
    task_type = str(policy.get("task_type", ""))
    cases = [c for c in evo_eval.load_evolution_cases()
             if str(c.get("expected_route", "")) == task_type]
    if len(cases) < max_cases:
        try:
            from ..trace import load_test_cases
            golden = [c for c in load_test_cases(tier="golden")
                      if str(c.get("expected_route", "")) == task_type
                      and c.get("input")]
            for g in golden:
                if len(cases) >= max_cases:
                    break
                if g.get("input") not in {c.get("input") for c in cases}:
                    cases.append({"id": g.get("id", g["input"][:20]),
                                  "input": g["input"],
                                  "expected_route": task_type})
        except Exception:
            pass
    # 兜底：route 匹配一条都没有 → 用全部 evolution case（至少能跑行为回归）
    if not cases:
        cases = [c for c in evo_eval.load_evolution_cases() if c.get("input")]
    return cases[:max_cases]


def _safe_run(case: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return evo_eval._run_case(case), None
    except Exception as exc:
        return None, str(exc)[:200]


def run_policy_ab(policy: dict[str, Any], max_cases: int | None = None) -> dict[str, Any]:
    """对候选策略跑晋升 A/B。返回 {pass: True|False|None, ...}。"""
    if max_cases is None:
        max_cases = int(meta.get_param("ab_max_cases", 6))
    cases = select_cases(policy, max_cases)
    if not cases:
        return {"pass": None, "reason": "无可用评测用例（evolution/golden 均为空）",
                "cases_used": 0}

    policy_id = str(policy.get("policy_id", "unknown"))
    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "type": "policy_ab_gate",
        "policy_id": policy_id,
        "task_type": policy.get("task_type", ""),
        "action": str(policy.get("action", ""))[:200],
        "case_count": len(cases),
    }

    errors: list[str] = []
    with isolate_agent_data():
        # ── 阶段 A：干净基线 ──
        phase_a: list[dict[str, Any]] = []
        for c in cases:
            r, err = _safe_run(c)
            if err:
                errors.append(f"A:{err}")
                continue
            phase_a.append(r)
        # ── 阶段 B：隔离区模拟固化（skill 文件 + 索引都写在隔离区）──
        from . import update
        try:
            update._promote_to_skill(copy.deepcopy(policy))
        except Exception as exc:
            return {"pass": None, "reason": f"模拟固化失败: {exc}"[:200],
                    "cases_used": len(cases)}
        skill_injected = False
        try:
            block = update.build_evolution_block(task=str(policy.get("trigger", "")))
            skill_injected = bool(block and "## Skill:" in block)
        except Exception:
            pass
        phase_b: list[dict[str, Any]] = []
        for c in cases:
            r, err = _safe_run(c)
            if err:
                errors.append(f"B:{err}")
                continue
            phase_b.append(r)

    # LLM 不可用/用例异常 → 不可判定（调用方降级 promote_pending）
    if errors and (len(phase_a) < len(cases) or len(phase_b) < len(cases)):
        return {"pass": None, "reason": "; ".join(errors[:2]),
                "cases_used": len(cases)}

    agg_a, agg_b = evo_eval._aggregate_evol(phase_a), evo_eval._aggregate_evol(phase_b)
    report["phase_a"], report["phase_b"] = agg_a, agg_b
    report["skill_injected_in_b"] = skill_injected

    verdict: bool | None = None
    notes: list[str] = []
    if agg_a.get("count", 0) > 0 and agg_b.get("count", 0) > 0:
        delta_success = agg_b["success_rate"] - agg_a["success_rate"]
        report["success_delta_pct"] = round(delta_success, 1)
        verdict = delta_success >= SUCCESS_DROP_HARD_LIMIT
        notes.append(f"成功率 {agg_a['success_rate']}% → {agg_b['success_rate']}%")
        if agg_a.get("avg_latency_ms") and agg_b.get("avg_latency_ms"):
            lat = round((agg_b["avg_latency_ms"] - agg_a["avg_latency_ms"])
                        / agg_a["avg_latency_ms"] * 100, 1)
            report["latency_delta_pct"] = lat
            notes.append(f"latency {lat:+.1f}%")
        if agg_a.get("total_tokens") and agg_b.get("total_tokens"):
            tok = round((agg_b["total_tokens"] - agg_a["total_tokens"])
                        / agg_a["total_tokens"] * 100, 1)
            report["tokens_delta_pct"] = tok
            notes.append(f"tokens {tok:+.1f}%")
    report["notes"] = notes
    report["verdict"] = "pass" if verdict else ("fail" if verdict is False else "unknown")

    # 报告落盘（真实 benchmark 目录；文件名带 policy_id，避免同日覆盖）
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        safe = datetime.now().isoformat()[:10].replace("-", "_")
        path = _REPORT_DIR / f"policy_ab_{policy_id}_{safe}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        report["report_file"] = str(path.name)
    except Exception:
        logger.warning("evolve.ab.report_write_failed", policy_id=policy_id)

    return {"pass": verdict,
            "reason": "; ".join(notes) if verdict is not None else "指标不足",
            "success_delta_pct": report.get("success_delta_pct"),
            "latency_delta_pct": report.get("latency_delta_pct"),
            "tokens_delta_pct": report.get("tokens_delta_pct"),
            "cases_used": len(cases),
            "report_file": report.get("report_file", "")}


def run_policy_ab_spawn(policy_id: str, timeout_s: int = 900) -> dict[str, Any]:
    """子进程跑 A/B 门（后台线程安全路径）。

    isolate_agent_data 是进程级全局路径 patch：若在 daemon 线程内直跑 A/B，
    patch 窗口内的在线用户请求也会被重定向到临时目录（数据丢失风险）。
    子进程彻底规避竞态：spawn `python -m app.cli _abgate <policy_id>`，
    stdout 末行输出一段 JSON 结果。
    """
    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "app.cli", "_abgate", policy_id],
            capture_output=True, text=True, timeout=timeout_s, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return {"pass": None, "reason": f"A/B 子进程超时（>{timeout_s}s）"}
    except Exception as exc:
        return {"pass": None, "reason": f"子进程启动失败: {exc}"[:200]}

    if proc.returncode != 0:
        return {"pass": None,
                "reason": f"子进程退出码 {proc.returncode}: {(proc.stderr or '')[-200:]}"}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
                if "pass" in result:
                    return result
            except json.JSONDecodeError:
                continue
    return {"pass": None, "reason": "子进程输出不可解析"}
