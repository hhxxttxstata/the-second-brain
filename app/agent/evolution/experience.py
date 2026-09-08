"""Experience Layer — 原始 trace 的规范化、聚合与窗口对比。

自进化 L1 的数据底座：
  - 读取 agent_data/traces/*.json（两种格式：save_trace 的简版 / TraceRecord 的完整版）
  - 规范化为统一的 ExperienceRecord
  - 按 task_type 聚合指标（latency / tokens / 成功率）
  - 窗口对比（最近 N 次 vs 之前 N 次）→ 支撑 L2 的"更快更熟练成本更低"度量
  - 蒸馏游标（state.json）：记录上次蒸馏位置，避免重复处理
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_TRACES_DIR = settings.agent_data_dir / "traces"
_STATE_DIR = settings.agent_data_dir / "evolution"
_STATE_PATH = _STATE_DIR / "state.json"

# 窗口对比参数
RECENT_WINDOW = 5      # 最近 N 次
PREVIOUS_WINDOW = 5    # 之前 N 次（对比基线）
MIN_SAMPLES = 6        # 少于该样本数不做对比（噪声太大）

# 蒸馏阈值（runner 使用）
MIN_DISTILL_TRACES = 5       # 至少积累多少条未蒸馏 trace 才值得跑一次 LLM
MAX_DISTILL_INPUT = 12       # 单次蒸馏最多喂给 LLM 的 trace 数
AUTO_EVOLVE_MIN_INTERVAL_H = 12  # 自动进化最小间隔（小时）


def _ensure_state_dir() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR


# ---------------------------------------------------------------------------
# 状态持久化（蒸馏游标）
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# 进程内蒸馏游标写锁：后台 auto_evolve 线程与 API 线程并发蒸馏时，
# load→+1→save 的 read-modify-write 需要互斥（否则计数/游标被覆盖回退）
_STATE_LOCK = threading.Lock()


def save_state(state: dict[str, Any]) -> None:
    """原子写（temp + os.replace）+ 进程内锁，防并发覆盖与半写文件。"""
    _ensure_state_dir()
    with _STATE_LOCK:
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)


# ---------------------------------------------------------------------------
# Trace 读取与规范化
# ---------------------------------------------------------------------------

def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:26])
    except (ValueError, TypeError):
        return None


def _normalize(raw: dict[str, Any], mtime: float) -> dict[str, Any]:
    """把两种 trace 格式统一为 ExperienceRecord 字段。"""
    ts = _parse_ts(raw.get("timestamp", ""))
    intent = (raw.get("user_intent") or raw.get("subject")
              or raw.get("decision") or raw.get("type") or "")
    tool_calls = raw.get("tool_calls") or []
    return {
        "trace_id": raw.get("trace_id", ""),
        "task_type": str(raw.get("task_type", "unknown")),
        "timestamp": ts.isoformat() if ts else datetime.fromtimestamp(mtime).isoformat(),
        "ts": ts or datetime.fromtimestamp(mtime),
        "latency_ms": int(raw.get("latency_ms") or 0),
        "total_tokens": int(raw.get("total_tokens") or 0),
        "success": bool(raw.get("success", True)),
        "error": str(raw.get("error") or "")[:200],
        "intent": str(intent)[:100],
        "tool_count": len(tool_calls),
        "tool_names": [str(t.get("name", "")) for t in tool_calls][:8],
        "failure_codes": [str(c) for c in (raw.get("failure_codes") or [])][:5],
        "has_metrics": bool(raw.get("latency_ms") or raw.get("total_tokens")
                            or raw.get("tool_calls")),
    }


def load_traces(limit: int = 100) -> list[dict[str, Any]]:
    """读取最近的 trace，按时间倒序（最新在前）。"""
    if not _TRACES_DIR.exists():
        return []
    files = sorted(_TRACES_DIR.glob("trace_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    records: list[dict[str, Any]] = []
    for f in files[:limit]:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            records.append(_normalize(raw, f.stat().st_mtime))
        except (json.JSONDecodeError, OSError):
            continue
    # 按时间倒序（以文件 mtime 为准，个别 trace 无 timestamp 字段）
    records.sort(key=lambda r: r["ts"], reverse=True)
    return records


def get_undistilled(traces: list[dict[str, Any]],
                    state: dict[str, Any]) -> list[dict[str, Any]]:
    """返回上次蒸馏之后产生、且排除 benchmark 的 trace（按时间正序）。"""
    last = state.get("last_distilled_at", "")
    last_ts = _parse_ts(last)
    result = []
    for t in traces:
        if t["task_type"] == "benchmark":
            continue
        if last_ts is not None and t["ts"] <= last_ts:
            continue
        result.append(t)
    result.sort(key=lambda r: r["ts"])  # 正序：旧的先处理
    return result


# ---------------------------------------------------------------------------
# 聚合与窗口对比
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def aggregate(traces: list[dict[str, Any]], task_type: str | None = None) -> dict[str, Any]:
    """按 task_type 聚合指标（全部 trace 或指定类型）。"""
    pool = traces
    if task_type:
        pool = [t for t in traces if t["task_type"] == task_type]
    with_metrics = [t for t in pool if t["latency_ms"] > 0]
    with_tokens = [t for t in pool if t["total_tokens"] > 0]
    success = sum(1 for t in pool if t["success"])
    return {
        "task_type": task_type or "all",
        "count": len(pool),
        "success_rate": round(success / len(pool) * 100, 1) if pool else 0,
        "avg_latency_ms": _mean([t["latency_ms"] for t in with_metrics]),
        "avg_tokens": _mean([t["total_tokens"] for t in with_tokens]),
        "with_metrics": len(with_metrics),
    }


def compare_windows(traces: list[dict[str, Any]], task_type: str,
                    recent_n: int = RECENT_WINDOW,
                    previous_n: int = PREVIOUS_WINDOW) -> dict[str, Any]:
    """窗口对比：最近 N 次 vs 之前 N 次同类任务。

    Returns:
        {
          "task_type", "count", "verdict": improved|regressed|neutral|insufficient,
          "latency_delta_pct", "tokens_delta_pct", "success_delta",
          "recent": {...}, "previous": {...},
        }
    样本不足或没有可度量指标时 verdict=insufficient（不参与策略评分）。
    """
    # 只统计带度量指标的 trace：旧式简版 trace 无 latency/tokens（全 0），
    # 混入窗口会让信号失真（base=0 → 无信号，或 ±100% 虚假跳变）
    pool = sorted([t for t in traces if t["task_type"] == task_type and t.get("has_metrics")],
                  key=lambda r: r["ts"], reverse=True)
    if len(pool) < MIN_SAMPLES:
        return {"task_type": task_type, "count": len(pool),
                "verdict": "insufficient", "recent": {}, "previous": {}}

    recent = pool[:recent_n]
    previous = pool[recent_n:recent_n + previous_n]
    if not previous:
        return {"task_type": task_type, "count": len(pool),
                "verdict": "insufficient", "recent": {}, "previous": {}}

    def _window_stats(rows: list[dict]) -> dict:
        lat = [r["latency_ms"] for r in rows if r["latency_ms"] > 0]
        tok = [r["total_tokens"] for r in rows if r["total_tokens"] > 0]
        return {
            "avg_latency_ms": _mean(lat), "avg_tokens": _mean(tok),
            "success_rate": round(sum(1 for r in rows if r["success"]) / len(rows) * 100, 1),
            "n": len(rows),
        }

    rs, ps = _window_stats(recent), _window_stats(previous)

    def _delta(cur: float, base: float) -> float | None:
        if base <= 0:
            return None
        return round((cur - base) / base * 100, 1)

    latency_delta = _delta(rs["avg_latency_ms"], ps["avg_latency_ms"])
    tokens_delta = _delta(rs["avg_tokens"], ps["avg_tokens"])
    success_delta = rs["success_rate"] - ps["success_rate"]

    # 判定：只有具备可度量信号才算数
    signals = 0
    improved = 0
    regressed = 0
    if latency_delta is not None and rs["avg_latency_ms"] > 0:
        signals += 1
        if latency_delta <= -10:
            improved += 1
        elif latency_delta >= 10:
            regressed += 1
    if tokens_delta is not None and rs["avg_tokens"] > 0:
        signals += 1
        if tokens_delta <= -10:
            improved += 1
        elif tokens_delta >= 10:
            regressed += 1
    if rs["success_rate"] != ps["success_rate"]:
        signals += 1
        if success_delta > 0:
            improved += 1
        elif success_delta < 0:
            regressed += 1

    if signals == 0:
        verdict = "insufficient"
    elif improved > regressed:
        verdict = "improved"
    elif regressed > improved:
        verdict = "regressed"
    else:
        verdict = "neutral"

    return {
        "task_type": task_type, "count": len(pool),
        "verdict": verdict,
        "latency_delta_pct": latency_delta, "tokens_delta_pct": tokens_delta,
        "success_delta": success_delta,
        "recent": rs, "previous": ps,
    }


# ---------------------------------------------------------------------------
# 状态统计（CLI status 用）
# ---------------------------------------------------------------------------

def status_summary() -> dict[str, Any]:
    traces = load_traces(limit=200)
    state = load_state()
    by_type: dict[str, int] = {}
    for t in traces:
        by_type[t["task_type"]] = by_type.get(t["task_type"], 0) + 1
    undistilled = get_undistilled(traces, state)
    return {
        "trace_count": len(traces),
        "by_task_type": by_type,
        "undistilled_count": len(undistilled),
        "last_distilled_at": state.get("last_distilled_at", "(never)"),
        "distill_count": state.get("distill_count", 0),
        "auto_evolve_count": state.get("auto_evolve_count", 0),
        "last_auto_evolve_at": state.get("last_auto_evolve_at", "(never)"),
    }
