"""Agent Data Service — 基于 SQLite 的记忆存储和上下文构建。

职责边界:
  vault/      ← 人写的知识资产（.md），Agent 只读
  agent_data/ → Agent 自己的运行数据（SQLite memory.db），Agent 读写

2026-07-29 从 JSON 迁移到 SQLite。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

from .memory_store import (
    add_memory as sql_add_memory,
    build_context as sql_build_context,
    get_profile as sql_get_profile,
    get_recent_memories,
    search_memories as sql_search_memories,
    update_profile as sql_update_profile,
    get_all_todos,
    get_plan_history,
    save_task_todos,
    save_plan_history,
    init_db,
)

_DATA_DIR = settings.agent_data_dir

# ── 初始化 ──

init_db()


# ── 兼容层：保持旧函数签名，底层用 SQLite ──


def read_memory(memory_type: str) -> dict[str, Any]:
    """读取记忆（兼容旧接口）。"""
    if memory_type == "stable_profile":
        return sql_get_profile()
    elif memory_type == "episodic":
        entries = get_recent_memories("episodic", limit=100)
        return {"entries": entries}
    elif memory_type == "task":
        todos = get_all_todos()
        plans = get_plan_history(limit=50)
        return {"todos": todos, "history": plans}
    return {}


def write_memory(memory_type: str, data: dict[str, Any],
                 merge: bool = True) -> None:
    """写入记忆（兼容旧接口）。"""
    if memory_type == "stable_profile":
        sql_update_profile(data)
    elif memory_type == "episodic":
        for entry in data.get("entries", []):
            content = entry.get("content", "")
            if content:
                sql_add_memory(
                    content=content, memory_type="episodic",
                    tags=entry.get("tags", []), importance=3,
                    source="write_memory",
                )
    elif memory_type == "task":
        todos = data.get("todos", [])
        if todos:
            save_task_todos(todos)
        for h in data.get("history", []):
            pid = h.get("plan_id", "")
            if pid and not pid.startswith("taskop"):
                save_plan_history(
                    plan_id=pid, date=h.get("date", ""),
                    summary=h.get("summary", ""), items=h.get("items", []),
                )


def _deduplicate_todos(todos: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for t in todos:
        title = t.get("title", "").strip()
        if not title:
            continue
        if title in seen:
            existing = seen[title]
            prio_order = {"high": 3, "medium": 2, "low": 1}
            if prio_order.get(t.get("priority", "medium"), 2) > prio_order.get(existing.get("priority", "medium"), 2):
                existing["priority"] = t["priority"]
            if t.get("status", "pending") != "pending":
                existing["status"] = t["status"]
        else:
            seen[title] = dict(t)
    return list(seen.values())


def search_episodic(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """从 SQLite 搜索情景记忆。"""
    return sql_search_memories(query=query, memory_type="episodic", limit=limit)


def add_episodic(content: str, tags: list[str] | None = None) -> None:
    """追加一条情景记忆。"""
    sql_add_memory(content, memory_type="episodic", tags=tags or [],
                   importance=3, source="add_episodic")


# ── Stable Profile 更新 ──

PROFILE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"称呼.*?(?:为|叫)\s*(\S+)"), "称谓"),
    (re.compile(r"(?:以|叫)后.*?叫我\s*(\S+)"), "称谓"),
    (re.compile(r"(?:以后|每次).*?称呼"), "_generic_instruction"),
]


def is_profile_update(text: str) -> tuple[bool, str, str | None]:
    for pattern, field in PROFILE_PATTERNS:
        m = pattern.search(text)
        if m and field != "_generic_instruction":
            return True, field, m.group(1)
        if m and field == "_generic_instruction":
            return True, "_llm_resolve", None
    return False, "", None


def format_profile() -> str:
    data = sql_get_profile()
    if not data:
        return "(暂无用户画像)"
    items = [(k, v) for k, v in data.items()
             if not k.startswith("__") and not k.startswith("_") and v]
    if not items:
        return "(暂无用户画像)"
    return "\n".join(f"- {k}: {v}" for k, v in items)


def format_episodic(limit: int = 10) -> str:
    entries = get_recent_memories("episodic", limit=limit)
    if not entries:
        return "(暂无近期记忆)"
    lines = []
    for e in entries:
        ts = e.get("created_at", "")[:10]
        content = e.get("content", "")
        tags = json.loads(e["tags"]) if isinstance(e["tags"], str) else e.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"- [{ts}]{tag_str} {content[:200]}")
    return "\n".join(lines)


def format_tasks() -> str:
    todos = get_all_todos()
    plans = get_plan_history(limit=3)
    lines = []
    if todos:
        lines.append("待办:")
        for t in todos[-10:]:
            title = t.get('title', '')
            desc = t.get('description', '')
            if desc:
                lines.append(f"  - [ ] {title} ({t.get('priority', 'medium')}) — {desc[:120]}")
            else:
                lines.append(f"  - [ ] {title} ({t.get('priority', 'medium')})")
    if plans:
        lines.append("历史计划:")
        for p in plans:
            items = p.get("items", [])
            lines.append(f"  - {p.get('summary', '')}")
            for it in items[:5]:
                desc = it.get("description", "")
                if desc:
                    lines.append(f"    · {it.get('title', '')} — {desc[:200]}")
    return "\n".join(lines) if lines else "(暂无任务记忆)"


# ── Trace Layer（保持 JSON） ──

def _ensure_dir(subpath: str) -> Path:
    p = _DATA_DIR / subpath
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_trace(task_type: str, data: dict[str, Any]) -> str:
    import uuid
    trace_id = f"trace_{uuid.uuid4().hex[:10]}"
    path = _ensure_dir("traces") / f"{trace_id}.json"
    record = {"trace_id": trace_id, "task_type": task_type,
              "timestamp": datetime.now().isoformat(), **data}
    _save_json(path, record)
    return trace_id


def list_traces(task_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    traces_dir = _DATA_DIR / "traces"
    if not traces_dir.exists():
        return []
    files = sorted(traces_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for f in files[:limit]:
        t = _load_json(f)
        if task_type and t.get("task_type") != task_type:
            continue
        results.append(t)
    return results


# ── State Layer（保持 JSON） ──

def read_today_state() -> dict[str, Any]:
    today = date.today().isoformat()
    path = _DATA_DIR / "state" / f"{today}.json"
    return _load_json(path)


def save_today_state(data: dict[str, Any]) -> None:
    today = date.today().isoformat()
    path = _DATA_DIR / "state" / f"{today}.json"
    _ensure_dir("state")
    existing = _load_json(path)
    existing.update(data)
    existing["__date"] = today
    existing["__updated_at"] = datetime.now().isoformat()
    _save_json(path, existing)


# ── Context Builder（SQLite 版） ──

def build_context(task: str = "", max_tokens: int = 3000,
                   session_id: str | None = None,
                   user_id: str = "default_user") -> dict[str, Any]:
    """使用 SQLite 智能召回的 Context Builder。"""
    return sql_build_context(task=task, max_tokens=max_tokens,
                             session_id=session_id, user_id=user_id)
