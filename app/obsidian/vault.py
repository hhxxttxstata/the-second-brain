"""Obsidian vault service — 纯文件读写，替代所有数据库和向量存储。

架构:
  Agent 指令 → vault 读 .md → LLM 上下文 → Agent 指令 → vault 写 .md
"""
from __future__ import annotations

import fnmatch
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

VAULT_ROOT = Path(settings.obsidian_vault)


# ---------------------------------------------------------------------------
# 文件夹映射
# ---------------------------------------------------------------------------

FOLDER_RULES: dict[str, str] = {
    "diaries": "日记：每天的个人思考、经历、TODO。文件名格式 YYYY-MM-DD.md",
    "notes": "笔记：技术笔记、项目笔记、通用知识",
    "habbits": "兴趣习惯：信仰、历史、哲学、英语、运动、音乐",
    "知识沉淀": "深度知识：Agent系统设计、RAG技术、项目对比",
    "Clippings": "剪藏：从网页、视频保存的文章",
}

# 读取时的优先级（先读最重要的）
FOLDER_PRIORITY = [
    "diaries",
    "notes",
    "habbits",
    "知识沉淀",
    "Clippings",
]


def _read_file(path: Path, max_chars: int = 20000) -> str | None:
    """读取一个 .md 文件，返回内容（含 frontmatter）。"""
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            return text[:max_chars] + f"\n\n... (truncated at {max_chars} chars)"
        return text
    except Exception:
        return None


def _get_frontmatter(text: str) -> dict[str, Any]:
    """提取 YAML frontmatter 为简单字典。"""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result: dict[str, Any] = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


# ---------------------------------------------------------------------------
# 公共 API — vault 是用户知识资产库，Agent 只读
# ---------------------------------------------------------------------------


def vault_structure() -> str:
    """返回 Obsidian  vault 的文件夹结构概览。"""
    lines = ["## Obsidian Vault 结构\n"]
    for folder in FOLDER_PRIORITY:
        fp = VAULT_ROOT / folder
        if fp.is_dir():
            md_files = sorted(fp.glob("*.md"))
            instructions = FOLDER_RULES.get(folder, "")
            lines.append(f"\n📁 **{folder}/** — {instructions}")
            for f in md_files:
                if f.name != "instructions.md":
                # 显示文件名和最后修改时间
                    mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
                    lines.append(f"  📄 {f.stem} ({mtime})")
                    if f.stem == "instructions":
                        lines[-1] = f"  📜 {f.name} — 本文件夹规则"

    # claude.md（主档案）
    claude = VAULT_ROOT / "claude.md"
    if claude.exists():
        lines.append(f"\n📄 **claude.md** — 用户主档案")

    return "\n".join(lines)


def search_notes(keyword: str, folder: str | None = None,
                 max_results: int = 10, chars_per_match: int = 200) -> str:
    """全文搜索 .md 文件中的关键词。

    Args:
        keyword: 搜索关键词
        folder: 限定文件夹（diaries/notes/habbits/知识沉淀/Clippings）
        max_results: 最多返回条数
        chars_per_match: 每段上下文最大字符数
    """
    folders = [VAULT_ROOT / folder] if folder else [VAULT_ROOT / f for f in FOLDER_PRIORITY]
    results: list[dict] = []

    for folder_path in folders:
        if not folder_path.is_dir():
            continue
        for f in sorted(folder_path.glob("*.md")):
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            # 简单行级搜索
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if keyword.lower() in line.lower():
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    snippet = "\n".join(lines[start:end])[:chars_per_match]
                    results.append({
                        "file": f"{folder_path.name}/{f.name}",
                        "line": i + 1,
                        "snippet": snippet,
                    })
                    if len(results) >= max_results * 3:  # 收集足够再截断
                        break
            if len(results) >= max_results * 3:
                break

    if not results:
        return f"没有找到包含「{keyword}」的笔记。"

    # 去重折叠（同一文件多条只保留最相关的 2 条）
    seen_files: dict[str, int] = {}
    deduped: list[dict] = []
    for r in results:
        fname = r["file"]
        seen_files[fname] = seen_files.get(fname, 0) + 1
        if seen_files[fname] <= 2:
            deduped.append(r)
        if len(deduped) >= max_results:
            break

    lines = [f"## 搜索: 「{keyword}」 ({len(deduped)} 条结果)\n"]
    for r in deduped:
        lines.append(f"\n📁 **{r['file']}** (L{r['line']})")
        lines.append(f"```\n{r['snippet']}\n```")
    return "\n".join(lines)


def read_folder(folder: str, file_filter: str | None = None,
                max_files: int = 10, max_chars_per_file: int = 8000) -> str:
    """读取某个文件夹下的所有 .md 文件（按修改时间排序）。

    Args:
        folder: 文件夹名（diaries/notes/habbits/知识沉淀）
        file_filter: 可选文件名过滤（支持 glob）
        max_files: 最多读取文件数
        max_chars_per_file: 每个文件最大字符数
    """
    folder_path = VAULT_ROOT / folder
    if not folder_path.is_dir():
        return f"文件夹「{folder}」不存在。"

    files = sorted(folder_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if file_filter:
        files = [f for f in files if fnmatch.fnmatch(f.name, file_filter)]

    # instructions.md 总是优先
    instr = folder_path / "instructions.md"
    result_parts: list[str] = [f"## 📁 {folder}/\n"]
    if instr.exists():
        instr_text = _read_file(instr, max_chars=4000)
        if instr_text:
            result_parts.append(f"📜 **规则**:\n{instr_text}\n")

    count = 0
    for f in files:
        if f.name == "instructions.md":
            continue
        if count >= max_files:
            result_parts.append(f"\n...(还有更多文件，只展示了前 {max_files} 个)")
            break
        content = _read_file(f, max_chars=max_chars_per_file)
        if content is None:
            continue
        result_parts.append(f"\n## 📄 {f.stem}\n{content}")
        count += 1

    return "\n".join(result_parts)


def read_file(rel_path: str, max_chars: int = 20000) -> str:
    """读取一个特定的 .md 或 .csv 文件。

    Args:
        rel_path: 相对 vault 的路径，如 "notes/ai-agent-design.md"
        max_chars: 最大字符数
    """
    full_path = VAULT_ROOT / rel_path
    if not full_path.exists():
        return f"文件不存在: {rel_path}"
    content = _read_file(full_path, max_chars=max_chars)
    if content is None:
        return f"无法读取: {rel_path}"
    return content


def get_user_profile(max_chars: int = 4000) -> str:
    """读取 claude.md 用户档案。"""
    claude_path = VAULT_ROOT / "claude.md"
    content = _read_file(claude_path, max_chars=max_chars)
    if content is None:
        return "(未找到 claude.md 档案文件)"
    return f"## 用户档案 (claude.md)\n{content}"


def get_today_context() -> str:
    """获取今日上下文——今天的日记 + 最近日记。"""
    today = date.today()
    parts: list[str] = []

    # 1. 今天的日记
    ymd = today.strftime("%Y-%m-%d")
    for fmt in [ymd, f"{today.year}-{today.month}-{today.day}"]:
        diary = VAULT_ROOT / "diaries" / f"{fmt}.md"
        content = _read_file(diary, max_chars=10000)
        if content:
            parts.append(f"## 今日日记 ({fmt})\n{content}")
            break
    else:
        parts.append("(今天还没有日记)")

    # 2. 最近 3 篇日记
    diary_dir = VAULT_ROOT / "diaries"
    if diary_dir.is_dir():
        diaries = sorted(diary_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        count = 0
        for d in diaries:
            if d.name == "instructions.md":
                continue
            if d.stem == today.strftime("%Y-%m-%d"):
                continue  # 今日已读
            if count >= 3:
                break
            content = _read_file(d, max_chars=4000)
            if content:
                parts.append(f"\n## 📅 日记 {d.stem}\n{content}")
                count += 1

    # 3. 最新 habbits 更新
    habbits_dir = VAULT_ROOT / "habbits"
    if habbits_dir.is_dir():
        for f in sorted(habbits_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
            if f.name == "instructions.md":
                continue
            content = _read_file(f, max_chars=3000)
            if content:
                parts.append(f"\n## 🏃 {f.stem}\n{content}")

    return "\n".join(parts)


# NOTE: vault 是用户知识资产库，Agent 只读不写。
# 写操作（记忆/轨迹/状态）请走 app/agent/agent_data_service.py
# 如需读取 vault 文件，请使用本模块的 search_notes / read_folder / read_file 函数。


# ═══════════════════════════════════════════════════════════════════
# Vault 写操作 — 统一入口，含 Humman-in-the-Loop 审批
# ═══════════════════════════════════════════════════════════════════

def append_to_file(rel_path: str, content: str) -> str:
    """追加内容到 vault 文件末尾。"""
    vault_path = Path(settings.obsidian_vault)
    full = vault_path / rel_path
    if not full.exists():
        return f"❌ 文件不存在: {rel_path}"
    try:
        from app.core.logging import logger
        full.write_text(full.read_text(encoding="utf-8") + "\n" + content, encoding="utf-8")
        logger.info("vault.append", path=rel_path, chars=len(content))
        return f"✅ 已追加到 {rel_path}"
    except Exception as exc:
        return f"❌ 写入失败: {exc}"


def write_file(rel_path: str, content: str) -> str:
    """覆盖写入 vault 文件。"""
    vault_path = Path(settings.obsidian_vault)
    full = vault_path / rel_path
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        from app.core.logging import logger
        logger.info("vault.write", path=rel_path, chars=len(content))
        return f"✅ 已写入 {rel_path}"
    except Exception as exc:
        return f"❌ 写入失败: {exc}"

