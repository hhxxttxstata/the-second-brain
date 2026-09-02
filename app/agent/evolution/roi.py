"""ROI — 自进化回报度量（P5）：学习曲线与进化健康度。

数据源：
  - ledger.jsonl（近 N 天事件：promoted/retired/ab_gate/distill/meta）
  - benchmark/evolution_*.json（演化 suite 报告的 latency delta，取最近 3 份）
  - meta_stats.json（按 prompt 版本聚合的蒸馏健康度）

消费方：scorecard 的 L8 维度（score_evolution_roi）与 `evolve status` 的 ROI 摘要
（collect_roi）。无任何自进化数据时维度返回 None 自动跳过加权。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings

_REPORT_DIR = settings.agent_data_dir / "benchmark"


def collect_roi(days: int = 30) -> dict[str, Any]:
    """汇总自进化 ROI 原始指标（不做打分）。"""
    from . import ledger, meta

    since = (datetime.now() - timedelta(days=days)).isoformat()
    events = [e for e in ledger.read_ledger(limit=1000)
              if str(e.get("ts", "")) >= since]

    promoted = sum(1 for e in events if e.get("event") == "policy_promoted")
    retired = sum(1 for e in events if e.get("event") == "policy_retired")
    ab = [e for e in events if e.get("event") == "policy_ab_gate"]
    ab_pass = sum(1 for e in ab if e.get("verdict") == "pass")
    ab_fail = sum(1 for e in ab if e.get("verdict") == "fail")
    distill_runs = sum(1 for e in events if e.get("event") == "distill_run")
    meta_updates = sum(1 for e in events if e.get("event") == "meta_update")
    prompt_rollbacks = sum(1 for e in events
                           if e.get("event") == "meta_prompt_rollback")
    snapshots = len(ledger.list_snapshots())

    # 演化 suite 报告：最近 3 份的 latency delta（学习曲线斜率的代理）
    latency_deltas: list[float] = []
    try:
        for f in sorted(_REPORT_DIR.glob("evolution_*.json"), reverse=True)[:3]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                d = data.get("latency_delta_pct")
                if isinstance(d, (int, float)):
                    latency_deltas.append(float(d))
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass

    # 蒸馏健康度（全量 meta_stats，按版本聚合）
    try:
        stats = meta._load_stats()
        runs = sum(int(v.get("runs", 0)) for v in (stats.get("versions") or {}).values())
        parse_fail = sum(int(v.get("parse_fail", 0)) for v in (stats.get("versions") or {}).values())
    except Exception:
        runs, parse_fail = distill_runs, 0
    try:
        prompt_version = str((meta._load_config().get("prompt") or {}).get("distill_version", "v1"))
        meta_revision = int(meta._load_config().get("revision", 0))
    except Exception:
        prompt_version, meta_revision = "v1", 0

    return {
        "window_days": days,
        "promoted": promoted, "retired": retired,
        "ab_pass": ab_pass, "ab_fail": ab_fail,
        "distill_runs": runs, "parse_fail": parse_fail,
        "meta_updates": meta_updates, "prompt_rollbacks": prompt_rollbacks,
        "snapshots": snapshots,
        "latency_deltas": latency_deltas,
        "latency_delta_avg": (round(sum(latency_deltas) / len(latency_deltas), 1)
                              if latency_deltas else None),
        "prompt_version": prompt_version, "meta_revision": meta_revision,
    }


def score_evolution_roi() -> dict[str, Any]:
    """scorecard L8 维度：A/B 通过率(35) + 蒸馏健康(25) + 学习曲线(25) + 晋升健康(15)。"""
    roi = collect_roi()
    ab_total = roi["ab_pass"] + roi["ab_fail"]

    # 无任何自进化数据 → 维度跳过（不空转加分）
    if roi["distill_runs"] == 0 and roi["promoted"] == 0 and not roi["latency_deltas"]:
        return {"score": None, "detail": "暂无自进化数据（维度自动跳过）"}

    score = 0.0
    parts: list[str] = []

    if ab_total:
        s = 100.0 * roi["ab_pass"] / ab_total
        parts.append(f"A/B 通过率 {roi['ab_pass']}/{ab_total}")
    else:
        s = 70.0  # 尚无候选过门 → 中性
    score += 0.35 * s

    if roi["distill_runs"]:
        s = 100.0 * (1 - roi["parse_fail"] / max(roi["distill_runs"], 1))
        parts.append(f"蒸馏成功率 {s:.0f}%（{roi['distill_runs']} 批）")
    else:
        s = 50.0
    score += 0.25 * s

    if roi["latency_deltas"]:
        avg = roi["latency_delta_avg"]
        s = (100.0 if avg <= -5 else 70.0 if avg <= 0
             else 50.0 if avg <= 5 else 20.0)
        parts.append(f"演化 latency Δ {avg:+.1f}%")
    else:
        s = 50.0
    score += 0.25 * s

    p, r = roi["promoted"], roi["retired"]
    s = 100.0 if (p and r <= p * 2) else 60.0 if p else (20.0 if r else 50.0)
    parts.append(f"固化 {p} / 退役 {r}")
    score += 0.15 * s

    return {"score": round(score, 1), "detail": "；".join(parts)}
