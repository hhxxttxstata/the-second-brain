"""Read-only view-model endpoints for the /workspace UI.

仅做只读聚合：调用既有读函数（trace / memory_store / handoff /
failure_taxonomy / context_pressure）并映射为前端视图模型。
不修改任何 Agent 执行逻辑（LangGraph 流程、记忆写入、工具注册均不动）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import feedback as feedback_store
from app.agent import memory_store
from app.agent.context_pressure import measure_pressure
from app.agent.failure_taxonomy import compute_failure_distribution
from app.agent.handoff import get_active_handoffs
from app.agent.session import get_default_session
from app.agent.trace import _TRACE_DIR, get_latest_trace, get_trace_stats, load_all_traces

router = APIRouter(prefix="/workspace", tags=["Workspace"])

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_DATA = _REPO_ROOT / "agent_data"

_MEMORY_TYPES = ("stable_profile", "episodic", "task", "task_todos", "task_plan", "conversation")


def _load_latest_json(directory: Path, pattern: str) -> dict[str, Any] | None:
    """按修改时间取目录下最新一个 JSON 文件；不存在或损坏返回 None。"""
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def _load_candidates() -> list[dict[str, Any]]:
    """candidate 池：每个文件是单个对象或对象列表，统一展平。"""
    cdir = _AGENT_DATA / "eval" / "candidate"
    items: list[dict[str, Any]] = []
    if not cdir.exists():
        return items
    for f in sorted(cdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict):
                # trace_snapshot 可能很大，前端列表用不到
                entry.pop("trace_snapshot", None)
                items.append(entry)
    return items


def _trim_benchmark(bench: dict[str, Any] | None) -> dict[str, Any] | None:
    if not bench:
        return None
    results = bench.get("results") or []
    trimmed = []
    for r in results[:20]:
        if isinstance(r, dict):
            trimmed.append({k: v for k, v in r.items() if k != "final_output"})
    out = dict(bench)
    out["results"] = trimmed
    return out


def _today_memories_updates(traces: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    """今天所有 run 产生的记忆变化（来自 trace.memory_updates）。"""
    deltas: list[dict[str, Any]] = []
    for t in traces:
        ts = str(t.get("timestamp", ""))
        if not ts.startswith(today):
            continue
        for mu in t.get("memory_updates", []):
            deltas.append({
                "operation": "create",
                "type": mu.get("type", ""),
                "content": mu.get("preview", ""),
                "source_run_id": t.get("trace_id", ""),
                "timestamp": mu.get("timestamp", ts),
            })
    return deltas


@router.get("/summary")
def workspace_summary() -> dict[str, Any]:
    """工作台一次拉全量：系统状态 / 今日计数 / 任务 / 记忆 / 评测 / 压力。"""
    today = datetime.now().strftime("%Y-%m-%d")

    traces = load_all_traces(limit=60)
    stats = get_trace_stats(traces)
    # 简版 trace 无 success 字段默认成功（get_trace_stats 会误判为失败，这里重算）
    explicit_failed = sum(1 for t in traces if t.get("success", True) is False)
    stats["workspace_success_rate"] = round(
        (len(traces) - explicit_failed) / len(traces) * 100, 1) if traces else 100.0

    today_traces = [t for t in traces if str(t.get("timestamp", "")).startswith(today)]
    # 简版 trace 无 success 字段 → 默认成功（与 failure_taxonomy 口径一致）
    failed_today = [t for t in today_traces if t.get("success", True) is False]
    today_stats = {
        "active_tasks": 0,  # 下面由 handoff 填充
        "memory_updates": sum(len(t.get("memory_updates", [])) for t in today_traces),
        "reflections": sum(1 for t in today_traces if t.get("task_type") == "reflect"),
        "failed_runs": len(failed_today),
        "total_runs": len(today_traces),
    }

    try:
        active_tasks = get_active_handoffs()
    except Exception:
        active_tasks = []
    today_stats["active_tasks"] = len(active_tasks)

    recent_memories: list[dict[str, Any]] = []
    for mt in _MEMORY_TYPES:
        try:
            rows = memory_store.get_recent_memories(mt, limit=5)
        except Exception:
            rows = []
        for row in rows:
            recent_memories.append({
                "id": row.get("id"),
                "memory_type": mt,
                "content": row.get("content", ""),
                "importance": row.get("importance"),
                "source": row.get("source", ""),
                "created_at": row.get("created_at", ""),
            })
    recent_memories.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)
    recent_memories = recent_memories[:12]

    try:
        latest_run = get_latest_trace()
    except Exception:
        latest_run = None

    benchmark = _trim_benchmark(_load_latest_json(_AGENT_DATA / "benchmark", "benchmark_*.json"))
    multi_turn = _load_latest_json(_AGENT_DATA / "multi_turn", "multi_turn_*.json")
    scorecard = _load_latest_json(_AGENT_DATA / "scorecard", "v3_*.json")
    candidates = _load_candidates()

    try:
        failure_dist = compute_failure_distribution(traces)
    except Exception:
        failure_dist = {}

    try:
        pressure = measure_pressure(session_id=get_default_session())
    except Exception:
        pressure = None

    return {
        "system": {
            "status": "healthy",
            "trace_stats": stats,
        },
        "today": today_stats,
        "active_tasks": active_tasks,
        "recent_memories": recent_memories,
        "latest_run": latest_run,
        "evaluation": {
            "benchmark": benchmark,
            "multi_turn": multi_turn,
            "scorecard": scorecard,
            "candidates": candidates[:20],
            "failure_distribution": failure_dist,
            "memory_deltas_today": _today_memories_updates(traces, today)[:30],
        },
        "pressure": pressure,
    }


@router.get("/runs/{trace_id}")
def get_run(trace_id: str) -> dict[str, Any]:
    """按 trace_id 取单条完整 trace（供 Inspector 展开历史 run）。"""
    trace = _load_trace_by_id(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


def _load_trace_by_id(trace_id: str) -> dict[str, Any] | None:
    if not _TRACE_DIR.exists():
        return None
    for f in _TRACE_DIR.glob(f"{trace_id}.json"):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


class FeedbackRequest(BaseModel):
    trace_id: str
    failure_type: str          # useful / useless / tool_wrong / memory_wrong / ...
    input: str = ""            # 触发该轮回答的用户原话
    note: str = ""             # bad case 备注


@router.post("/feedback")
def submit_feedback(req: FeedbackRequest) -> dict[str, Any]:
    """记录用户反馈；负面反馈（bad case）自动回流 candidate 评测集。"""
    if req.failure_type not in feedback_store.FAILURE_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown failure_type: {req.failure_type}")

    trace_data = _load_trace_by_id(req.trace_id)
    fb = feedback_store.new_feedback(req.trace_id, req.failure_type, req.input, trace_data)
    if req.note.strip():
        fb["feedback_note"] = req.note.strip()[:500]
    feedback_store.save_feedback(fb)

    candidate_id: str | None = None
    if req.failure_type != "useful":
        candidate_id = _feedback_to_candidate(req, trace_data)

    return {"feedback_id": fb["feedback_id"], "candidate_id": candidate_id}


def _feedback_to_candidate(req: FeedbackRequest, trace_data: dict[str, Any] | None) -> str | None:
    """bad case 回流：写入 eval/candidate/，与 benchmark 自动捕获同 schema。

    幂等：同一 input 已在 candidate 池则不再重复回流（反馈本身仍保存）。
    """
    cand_dir = _AGENT_DATA / "eval" / "candidate"
    cand_dir.mkdir(parents=True, exist_ok=True)

    input_text = req.input.strip()
    existing: set[str] = set()
    for f in cand_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in data if isinstance(data, list) else [data]:
            if isinstance(c, dict) and c.get("input"):
                existing.add(str(c["input"]))
    if input_text in existing:
        return None

    now = datetime.now()
    cand = {
        "id": f"fb-{now.strftime('%Y%m%d%H%M%S')}",
        "intent": (input_text[:40] or (trace_data or {}).get("user_intent", "")[:40]),
        "input": input_text,
        "expected_route": "",
        "stage": "candidate",
        "tags": ["user_feedback", req.failure_type],
        "note": f"用户反馈回流 ({now.isoformat()[:10]})",
        "known_issue": f"用户反馈: {req.failure_type}" + (f" — {req.note.strip()[:200]}" if req.note.strip() else ""),
        "trace_ref": req.trace_id,
        "created_at": now.isoformat()[:10],
        "failure_codes": [f"USER_FEEDBACK_{req.failure_type.upper()}"],
    }
    path = cand_dir / f"feedback_{now.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
    return cand["id"]
