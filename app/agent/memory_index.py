"""Memory Index — MEMORY.md 索引式记忆管理。

生成 agent_data/memory/MEMORY.md 作为轻量索引清单，按 People / Preference / Project 分类。
每类只保留 3-5 条最重要/最新的摘要。
全文写在 SQLite 里，Index 只保留指针。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_INDEX_PATH = settings.agent_data_dir / "memory" / "MEMORY.md"


# ---------------------------------------------------------------------------
# 生成 MEMORY.md 索引
# ---------------------------------------------------------------------------


def _fetch_memories_for_index() -> dict[str, list[dict]]:
    """从 SQLite 读取各类记忆，按类型分组。"""
    from .memory_store import search_memories, get_profile, get_all_todos

    sections: dict[str, list[dict]] = {
        "people": [],
        "preference": [],
        "project": [],
        "todo": [],
        "fact": [],
    }

    # People: 带人物标签或包含'我叫/称呼/女朋友'的记忆
    people_q = search_memories(query="我叫 OR 女朋友 OR 称呼 OR 姓名", limit=5)
    for p in people_q:
        sections["people"].append({
            "content": p.get("content", "")[:120],
            "updated": p.get("created_at", "")[:10],
        })

    # Preferences: 带'喜欢|讨厌|偏好'的记忆
    pref_q = search_memories(query="喜欢 OR 讨厌 OR 偏好 OR 感兴趣", limit=5)
    for p in pref_q:
        sections["preference"].append({
            "content": p.get("content", "")[:120],
            "updated": p.get("created_at", "")[:10],
        })

    # Projects: 带'项目|目标|准备'的记忆
    proj_q = search_memories(query="项目 OR 目标 OR 秋招 OR offer", limit=5)
    for p in proj_q:
        sections["project"].append({
            "content": p.get("content", "")[:120],
            "updated": p.get("created_at", "")[:10],
        })

    # Todos
    todos = get_all_todos()
    for t in todos[:8]:
        sections["todo"].append({
            "content": t.get("title", "")[:120],
            "priority": t.get("priority", "medium"),
        })

    # Facts from profile
    profile = get_profile()
    if profile:
        name = profile.get("name", profile.get("称谓", ""))
        if name:
            sections["people"].append({"content": f"用户名称: {name}", "updated": "persistent"})

    return sections


def _format_index_section(title: str, items: list[dict],
                           fields: list[str]) -> str:
    """格式化一个索引分区。"""
    if not items:
        return ""
    lines = [f"### {title}"]
    for item in items:
        if "content" in item and item["content"]:
            rest = "".join(f" | {k}: {item[k]}" for k in fields if k in item and k != "content")
            lines.append(f"- {item['content']}{rest}")
    lines.append("")
    return "\n".join(lines)


def build_memory_index() -> str:
    """生成 MEMORY.md 索引文本。"""
    sections = _fetch_memories_for_index()

    lines = [
        f"# Memory Index",
        f"Generated: {datetime.now().isoformat()[:10]}",
        "",
        "This index summarizes what I know. Use `search_memories` to retrieve details.",
        "",
    ]

    people = _format_index_section("People", sections["people"], ["updated"])
    if people:
        lines.append(people)

    pref = _format_index_section("Preferences", sections["preference"], ["updated"])
    if pref:
        lines.append(pref)

    proj = _format_index_section("Projects", sections["project"], ["updated"])
    if proj:
        lines.append(proj)

    todo = _format_index_section("Active Tasks", sections["todo"], ["priority"])
    if todo:
        lines.append(todo)

    # Facts from profile
    from .memory_store import get_profile
    profile = get_profile()
    fact_lines = []
    for k, v in profile.items():
        if not k.startswith("_") and not k.startswith("__") and v:
            fact_lines.append(f"- {k}: {str(v)[:80]}")
    if fact_lines:
        lines.append("### Profile Fields")
        lines.extend(fact_lines)
        lines.append("")

    return "\n".join(lines)


def save_memory_index() -> str:
    """写入 MEMORY.md 到文件系统。"""
    content = build_memory_index()
    _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    _INDEX_PATH.write_text(content, encoding="utf-8")
    return str(_INDEX_PATH)


def load_memory_index() -> str:
    """读取 MEMORY.md 内容。"""
    if _INDEX_PATH.exists():
        return _INDEX_PATH.read_text(encoding="utf-8")[:1500]  # 截断到 1500 chars
    return build_memory_index()


def refresh_index_if_stale(max_age_hours: int = 6) -> str:
    """如果索引过旧则刷新。"""
    if not _INDEX_PATH.exists():
        return save_memory_index()
    mtime = datetime.fromtimestamp(_INDEX_PATH.stat().st_mtime)
    age = (datetime.now() - mtime).total_seconds() / 3600
    if age > max_age_hours:
        return save_memory_index()
    return load_memory_index()
