"""Session JSONL — 结构化会话转录 + 压缩摘要 + Carryover。

每个会话结束时写一条结构化 JSONL 记录。
tasks/ 和 handoffs/ 是 Source of Truth，JSONL 只是导航。

JSONL 结构:
  {
    "session_id": "...",
    "goal": "本轮核心目标",
    "decisions": ["做出的决策列表"],
    "completed": ["已完成事项"],
    "pending": [{"task_id":"...", "status":"...", "action":"..."}],
    "next_actions": ["下一步建议"],
    "evidence_refs": ["trace://...", "handoff://..."],
    "summary": "一句话摘要",
    "tool_count": 5
  }
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_SESSION_LOG_DIR = settings.agent_data_dir / "session_logs"
_CARRYOVER_COUNT = 3


def _ensure_dir() -> Path:
    _SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _SESSION_LOG_DIR


def log_session_summary(
    session_id: str,
    goal: str = "",
    decisions: list[str] | None = None,
    completed: list[str] | None = None,
    pending: list[dict[str, Any]] | None = None,
    next_actions: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    summary: str = "",
    tool_count: int = 0,
    resume: str = "",
) -> str:
    """写入一条结构化 session summary 到 JSONL。

    Args:
        session_id: 会话 ID
        goal: 本轮核心目标（一句话）
        decisions: 本轮做出的具体决策
        completed: 已完成事项
        pending: 未完成的 task handoffs
        next_actions: 下一步建议
        evidence_refs: 证据引用（trace:// handoff:// task://）
        summary: 一句话摘要
        tool_count: 工具调用次数
        resume: 显式 resume 指令（旧接口兼容）
    """
    _ensure_dir()
    date_prefix = session_id.split("_")[-1] if "_" in session_id else "unknown"
    path = _SESSION_LOG_DIR / f"{date_prefix}.jsonl"

    record: dict[str, Any] = {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
    }
    if goal:
        record["goal"] = goal[:200]
    if decisions:
        record["decisions"] = decisions[:10]
    if completed:
        record["completed"] = completed[:10]
    if pending:
        record["pending"] = pending[:5]
    if next_actions:
        record["next_actions"] = next_actions[:5]
    if evidence_refs:
        record["evidence_refs"] = evidence_refs[:5]
    if summary:
        record["summary"] = summary[:300]
    if tool_count:
        record["tool_count"] = tool_count
    if resume:
        record["resume"] = resume[:200]

    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


def get_session_logs(date_prefix: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """读取最近的 session 日志。"""
    _ensure_dir()
    files = sorted(_SESSION_LOG_DIR.glob("*.jsonl"), reverse=True)
    logs = []
    for f in files:
        if date_prefix and date_prefix not in f.stem:
            continue
        for line in f.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                logs.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                pass
        if len(logs) >= limit:
            break
    return logs[:limit]


def get_carryover_context(session_id: str) -> str:
    """获取跨会话延续上下文（文本版，供 build_context 注入）。

    读取最近 _CARRYOVER_COUNT 条不同会话的结构化 JSONL，
    排除当前 session，格式化为易读文本。
    """
    logs = get_session_logs(limit=_CARRYOVER_COUNT + 3)
    if not logs:
        return ""

    seen_sids: set[str] = set()
    lines: list[str] = []

    for log in logs:
        sid = log.get("session_id", "")
        if not sid or sid in seen_sids or sid == session_id:
            continue
        seen_sids.add(sid)
        if len(seen_sids) > _CARRYOVER_COUNT:
            break

        short_id = sid[-16:]

        # goal / summary
        goal = log.get("goal") or log.get("summary", "")
        if goal:
            lines.append(f"- 上一会话({short_id}): {goal[:150]}")

        # 决策
        decisions = log.get("decisions", [])
        for d in decisions[:2]:
            lines.append(f"  决策: {d[:100]}")

        # pending
        pending = log.get("pending", [])
        for p in pending:
            tid = p.get("task_id", "")
            action = p.get("action", "")
            status = p.get("status", "")
            lines.append(f"  ⏳ {tid}: {action} ({status})")

        # completed
        completed = log.get("completed", [])
        if completed:
            lines.append(f"  已完成: {'; '.join(c[:50] for c in completed[:2])}")

        # next_actions
        na = log.get("next_actions", [])
        for n in na[:1]:
            lines.append(f"  下一步: {n[:100]}")

    return "\n".join(lines) if lines else ""


def get_all_resume_context(session_id: str) -> str:
    """获取完整 Resume 上下文。"""
    logs = get_session_logs(limit=10)
    if not logs:
        return ""

    parts: list[str] = []
    seen_sids: set[str] = set()
    current = None
    previous = []

    for log in logs:
        sid = log.get("session_id", "")
        if not sid or sid in seen_sids:
            continue
        seen_sids.add(sid)

        entry = {
            "session_id": sid[-20:],
            "status": "当前会话" if sid == session_id else "历史会话",
            "goal": log.get("goal", log.get("summary", ""))[:200],
            "pending": log.get("pending", []),
            "decisions": log.get("decisions", []),
            "completed": log.get("completed", []),
            "evidence": log.get("evidence_refs", []),
        }
        if sid == session_id:
            current = entry
        else:
            previous.append(entry)

    if current:
        parts.append(f"## 当前会话状态")
        parts.append(f"  目标: {current['goal'][:120]}")
        if current["decisions"]:
            for d in current["decisions"][:3]:
                parts.append(f"  决策: {d[:80]}")
        if current["completed"]:
            for c in current["completed"][:3]:
                parts.append(f"  ✅ {c[:80]}")
        if current["pending"]:
            for p in current["pending"][:3]:
                parts.append(f"  ⏳ {p.get('task_id','')}: {p.get('action','')}")

    if previous:
        parts.append("## 历史会话延续")
        for p in previous:
            parts.append(f"  - [{p['session_id']}] {p['goal'][:120]}")
            if p["pending"]:
                for pp in p["pending"][:2]:
                    parts.append(f"    ⏳ {pp.get('task_id','')}: {pp.get('action','')}")

    return "\n".join(parts)


def get_carryover_summary(session_id: str) -> str:
    """保留旧接口。"""
    return get_carryover_context(session_id)


# ---------------------------------------------------------------------------
# 压缩工具
# ---------------------------------------------------------------------------


def truncate_old_logs(max_sessions: int = 50) -> int:
    """删除最旧的日志，保留最近 N 条。返回删除条数。"""
    all_logs = get_session_logs(date_prefix="", limit=max_sessions + 50)
    if len(all_logs) <= max_sessions:
        return 0

    # 按文件分组删除
    files = sorted(_SESSION_LOG_DIR.glob("*.jsonl"), reverse=True)
    to_remove = len(all_logs) - max_sessions
    removed = 0

    for f in reversed(files):
        if removed >= to_remove:
            break
        try:
            lines = f.read_text(encoding="utf-8").strip().split("\n")
            if len(lines) <= 1:
                f.unlink()
                removed += 1
            elif len(lines) <= removed:
                f.unlink()
                removed += len(lines)
            else:
                # 保留最后 max_sessions 行
                keep = lines[-(max_sessions - (to_remove - removed)):]
                f.write_text("\n".join(keep) + "\n", encoding="utf-8")
                removed += to_remove - removed
        except Exception:
            pass

    return removed
