"""Agent Data Service — 纯 JSON 文件读写 Agent 运行数据。

职责边界:
  vault/   ← 人写的知识资产（.md），Agent 只读
  agent_data/ ← Agent 自己的运行数据（.json），Agent 读写
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_DATA_DIR = settings.agent_data_dir


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Memory Layer — 三层记忆
# ---------------------------------------------------------------------------

MEMORY_FILES = {
    "stable_profile": "memory/profile.json",
    "episodic": "memory/episodic.json",
    "task": "memory/task_memory.json",
}


def read_memory(memory_type: str) -> dict[str, Any]:
    """读取对应的 memory JSON 文件。"""
    rel = MEMORY_FILES.get(memory_type)
    if not rel:
        return {}
    return _load_json(_DATA_DIR / rel)


def write_memory(memory_type: str, data: dict[str, Any],
                 merge: bool = True) -> None:
    """写入一条记忆到对应的 JSON 文件。

    Args:
        memory_type: "stable_profile" | "episodic" | "task"
        data: 要写入的数据
        merge: True=合并更新，False=覆盖
    """
    rel = MEMORY_FILES.get(memory_type)
    if not rel:
        raise ValueError(f"Unknown memory_type: {memory_type}")
    path = _DATA_DIR / rel
    _ensure_dir("memory")

    if merge and path.exists():
        existing = _load_json(path)
        existing.update(data)
        existing["__updated_at"] = datetime.now().isoformat()
        _save_json(path, existing)
    else:
        data["__created_at"] = datetime.now().isoformat()
        data["__updated_at"] = datetime.now().isoformat()
        _save_json(path, data)


def search_episodic(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """从 episodic 记忆中搜索最近的条目。"""
    data = read_memory("episodic")
    entries = data.get("entries", [])
    if not entries:
        return []

    # 按时间倒序，返回最近的 N 条
    entries_sorted = sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)
    query_lower = query.lower()
    if query_lower:
        matched = [e for e in entries_sorted if query_lower in json.dumps(e, ensure_ascii=False).lower()]
        return matched[:limit]
    return entries_sorted[:limit]


def add_episodic(content: str, tags: list[str] | None = None) -> None:
    """追加一条情景记忆。"""
    data = read_memory("episodic")
    if "entries" not in data:
        data["entries"] = []
    data["entries"].append({
        "content": content,
        "tags": tags or [],
        "timestamp": datetime.now().isoformat(),
    })
    # 最多保留 100 条
    if len(data["entries"]) > 100:
        data["entries"] = data["entries"][-100:]
    write_memory("episodic", data, merge=False)


def format_profile() -> str:
    """格式化 profile 供 LLM 上下文使用。"""
    data = read_memory("stable_profile")
    if not data:
        return "(暂无用户画像)"
    return "\n".join(f"- {k}: {v}" for k, v in data.items()
                     if not k.startswith("__"))


def format_episodic(limit: int = 10) -> str:
    """格式化情景记忆供 LLM 上下文使用。"""
    data = read_memory("episodic")
    entries = data.get("entries", [])[-limit:]
    if not entries:
        return "(暂无近期记忆)"
    lines = []
    for e in entries:
        ts = e.get("timestamp", "")[:10]
        content = e.get("content", "")
        tags = e.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"- [{ts}]{tag_str} {content[:200]}")
    return "\n".join(lines)


def format_tasks() -> str:
    """格式化任务记忆。"""
    data = read_memory("task")
    todos = data.get("todos", [])
    history = data.get("history", [])
    lines = []
    if todos:
        lines.append("待办:")
        for t in todos[-10:]:
            lines.append(f"  - [ ] {t.get('title', '')} ({t.get('priority', 'medium')})")
    if history:
        lines.append("历史计划:")
        for h in history[-5:]:
            lines.append(f"  - {h.get('date', '')}: {h.get('summary', '')}")
    return "\n".join(lines) if lines else "(暂无任务记忆)"


# ---------------------------------------------------------------------------
# Trace Layer
# ---------------------------------------------------------------------------

def save_trace(task_type: str, data: dict[str, Any]) -> str:
    """保存一条 Agent 运行轨迹到 traces/ 目录。

    Returns:
        trace_id
    """
    import uuid
    trace_id = f"trace_{uuid.uuid4().hex[:10]}"
    path = _ensure_dir("traces") / f"{trace_id}.json"
    record = {
        "trace_id": trace_id,
        "task_type": task_type,
        "timestamp": datetime.now().isoformat(),
        **data,
    }
    _save_json(path, record)
    return trace_id


def list_traces(task_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """列出最近的 traces。"""
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


# ---------------------------------------------------------------------------
# State Layer
# ---------------------------------------------------------------------------

def read_today_state() -> dict[str, Any]:
    """读取今日状态。"""
    today = date.today().isoformat()
    path = _DATA_DIR / "state" / f"{today}.json"
    return _load_json(path)


def save_today_state(data: dict[str, Any]) -> None:
    """保存/更新今日状态。"""
    today = date.today().isoformat()
    path = _DATA_DIR / "state" / f"{today}.json"
    _ensure_dir("state")
    existing = _load_json(path)
    existing.update(data)
    existing["__date"] = today
    existing["__updated_at"] = datetime.now().isoformat()
    _save_json(path, existing)


# ---------------------------------------------------------------------------
# Context Builder — 合并两层上下文
# ---------------------------------------------------------------------------

def build_context(task: str = "", max_tokens: int = 3000) -> dict[str, Any]:
    """Context Builder — 组装最终上下文给 LLM。

    Sources:
      1. agent_data/ → 结构化记忆/状态（直接读 JSON）
      2. vault/ → 知识资产（RAG 检索 .md 文件）
    """
    parts: list[str] = []
    sources: list[dict[str, Any]] = []

    # 1. 从 agent_data 读运行数据
    profile_text = format_profile()
    episodic_text = format_episodic(limit=10)
    tasks_text = format_tasks()
    today_state = read_today_state()

    if profile_text and profile_text != "(暂无用户画像)":
        parts.append(f"## 用户画像\n{profile_text}")
        sources.append({"layer": "agent_data", "type": "stable_profile"})
    if episodic_text and episodic_text != "(暂无近期记忆)":
        parts.append(f"## 近期记忆\n{episodic_text}")
        sources.append({"layer": "agent_data", "type": "episodic"})
    if tasks_text and tasks_text != "(暂无任务记忆)":
        parts.append(f"## 任务状态\n{tasks_text}")
        sources.append({"layer": "agent_data", "type": "task"})
    if today_state:
        formatted = "\n".join(f"- {k}: {v}" for k, v in today_state.items() if not k.startswith("__"))
        if formatted:
            parts.append(f"## 今日状态\n{formatted}")
            sources.append({"layer": "agent_data", "type": "state"})

    # 2. 从 vault 检索相关知识（仅 task 相关时检索）
    if task:
        from app.obsidian import vault as vault_service

        vault_results = vault_service.search_notes(task, max_results=3, chars_per_match=300)
        if vault_results and "没有找到" not in vault_results:
            parts.append(f"## 相关笔记\n{vault_results}")
            sources.append({"layer": "vault", "type": "rag"})

    return {
        "context": "\n\n".join(parts),
        "sources": sources,
    }
