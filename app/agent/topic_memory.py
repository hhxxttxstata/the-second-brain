"""Topic Memory — 索引式长期记忆，MEMORY.md + Topic Files。

结构:
  agent_data/memory/
  ├── MEMORY.md          ← 简洁索引（~500 chars），每轮注入
  ├── people/            ← 人物笔记
  ├── preferences.md     ← 用户偏好
  ├── projects/          ← 项目记忆
  ├── decisions.md       ← 重要决策记录
  └── lessons.md         ← 经验教训

MEMORY.md 只存指针/摘要，详情放在 Topic Files。
每一轮 session 加载 MEMORY.md 的前 30 行，按需读取 Topic Files。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_MEMORY_DIR = settings.agent_data_dir / "memory"
_INDEX_FILE = _MEMORY_DIR / "MEMORY.md"


def _ensure_dir() -> Path:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (_MEMORY_DIR / "people").mkdir(exist_ok=True)
    (_MEMORY_DIR / "projects").mkdir(exist_ok=True)
    return _MEMORY_DIR


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def read_index(max_lines: int = 30, max_chars: int = 1500) -> str:
    """读取 MEMORY.md 索引（前 N 行 / 前 N 字符）。

    这是每轮上下文注入的内容——短小精悍。
    """
    if not _INDEX_FILE.exists():
        return _generate_default_index()
    try:
        lines = _INDEX_FILE.read_text(encoding="utf-8").split("\n")
        text_lines = []
        char_count = 0
        for line in lines[:max_lines]:
            line_text = line.rstrip()
            text_lines.append(line_text)
            char_count += len(line_text) + 1
            if char_count > max_chars:
                text_lines.append("...(truncated)")
                break
        return "\n".join(text_lines)
    except Exception:
        return _generate_default_index()


def read_topic(topic_path: str) -> str:
    """读取一个 Topic File 的完整内容。

    topic_path: "people/zhang-san" 或 "preferences" 或 "projects/ai-agent"
    自动补全 .md 后缀。
    """
    path = _MEMORY_DIR / _normalize_path(topic_path)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"(Topic file not found: {topic_path})"


def search_topic(query: str) -> list[dict[str, Any]]:
    """在所有 Topic Files 中搜索关键词，返回匹配片段。"""
    results = []
    for f in _MEMORY_DIR.rglob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
            rel = f.relative_to(_MEMORY_DIR)
            for line_no, line in enumerate(content.split("\n"), 1):
                if query.lower() in line.lower():
                    results.append({
                        "file": str(rel),
                        "line": line_no,
                        "content": line.strip()[:200],
                    })
                    if len(results) >= 10:
                        return results
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def write_topic(topic_path: str, content: str, append: bool = False) -> str:
    """写入/追加一个 Topic File。

    自动规范化路径，补 .md 后缀。
    """
    _ensure_dir()
    path = _MEMORY_DIR / _normalize_path(topic_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    comment = f"---\n# updated: {datetime.now().isoformat()}\n---\n"
    if append and path.exists():
        with open(str(path), "a", encoding="utf-8") as f:
            f.write(f"\n\n{comment}{content}")
    else:
        path.write_text(f"{comment}{content}", encoding="utf-8")
    return str(path)


def upsert_index_entry(category: str, title: str, link: str, summary: str) -> None:
    """在 MEMORY.md 中增/更新一条索引条目。

    MEMORY.md 格式:
      ## People
      - 张三: 见 people/zhang-san.md | 前端开发，3年经验
      - 李四: 见 people/li-si.md | 设计

    Args:
        category: "People" | "Projects" | "Preferences" | "Decisions" | "Lessons"
        title: 条目标题
        link: Topic File 路径，如 "people/zhang-san"
        summary: 单行摘要（~100 chars）
    """
    _ensure_dir()
    link_text = f"见 {link}.md"

    if not _INDEX_FILE.exists():
        _write_full_index()

    lines = _INDEX_FILE.read_text(encoding="utf-8").split("\n")
    section_header = f"## {category}"
    section_start = -1
    section_end = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            section_start = i
        elif section_start >= 0 and stripped.startswith("## ") and i > section_start:
            section_end = i
            break

    entry_line = f"- {title}: {link_text} | {summary}"

    if section_start >= 0:
        # 检查是否已有同链接条目
        for i in range(section_start + 1, section_end):
            if link_text in lines[i]:
                lines[i] = entry_line
                _INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")
                return
        # 不存在则追加到 section 末尾
        lines.insert(section_end, entry_line)
        _INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")
    else:
        # section 不存在，在文件末尾追加
        lines.append(f"\n{section_header}\n{entry_line}")
        _INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 级联读取：MEMORY.md → 按需读 Topic
# ---------------------------------------------------------------------------


def load_relevant_memories(task: str, max_chars: int = 2000) -> str:
    """根据 task 关键词，读取 MEMORY.md + 相关 Topic Files。

    策略:
      1. 读 MEMORY.md 索引（前 30 行）
      2. 提取索引中的链接
      3. 根据 task 关键词匹配，只读相关的 Topic Files
      4. 拼接返回
    """
    parts = []
    index_text = read_index(max_lines=30, max_chars=800)
    parts.append(f"## Memory Index\n{index_text}")
    remaining = max_chars - len(index_text)
    if remaining <= 0:
        return "\n\n".join(parts)

    # 提取索引中的链接: "见 people/zhang-san.md"
    links = re.findall(r"见\s+([\w/.-]+\.md)", index_text)
    task_lower = task.lower()

    for link in links:
        if remaining <= 200:
            break
        topic_path = _MEMORY_DIR / link
        if not topic_path.exists():
            continue
        # 如果 task 关键词匹配到文件名或链接路径，才读取
        if not any(kw in link.lower() or kw in task_lower
                   for kw in (task_lower.split() + [link.split("/")[-1].replace(".md", "")])):
            continue
        try:
            content = topic_path.read_text(encoding="utf-8")
            if len(content) > remaining:
                content = content[:remaining] + "\n...(truncated)"
            parts.append(f"## {link}\n{content}")
            remaining -= len(content)
        except Exception:
            pass

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """规范化路径：补 .md 后缀，去多余符号。"""
    path = path.strip().replace("\\", "/")
    if not path.endswith(".md"):
        path += ".md"
    if path.startswith("/"):
        path = path[1:]
    return path


def _write_full_index() -> None:
    """写入初始的 MEMORY.md。"""
    _ensure_dir()
    content = """# Memory Index

_Last updated: {date}_

Contains pointers to detailed topic files. Use `read_topic_memory` or `search_topic_memory` to read full content.

## People

## Preferences

## Projects

## Decisions

## Lessons

---

_Add entries via `upsert_index_entry` or edit this file directly._
""".format(date=date.today().isoformat())
    _INDEX_FILE.write_text(content, encoding="utf-8")


def _generate_default_index() -> str:
    """如果 MEMORY.md 不存在时返回的默认索引。"""
    return """# Memory Index

(Empty — use `write_topic_memory` to create topic memories.)
"""


# ---------------------------------------------------------------------------
# 维护
# ---------------------------------------------------------------------------


def get_all_topics() -> list[dict[str, Any]]:
    """扫描所有 Topic Files 返回元信息。"""
    results = []
    for f in sorted(_MEMORY_DIR.rglob("*.md")):
        if f.name == "MEMORY.md":
            continue
        rel = f.relative_to(_MEMORY_DIR)
        stats = f.stat()
        results.append({
            "path": str(rel),
            "size": stats.st_size,
            "updated": datetime.fromtimestamp(stats.st_mtime).isoformat()[:10],
        })
    return results


def consolidate_topics() -> None:
    """合并/去重 Topic Files（placeholder — 后续用 LLM 压缩）。"""
    pass
