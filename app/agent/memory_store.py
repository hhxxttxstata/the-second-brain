"""SQLite 记忆存储 — 替代 JSON 文件，提供结构化查询能力。

Schema:
  memories    — 所有记忆类型（profile/episodic/task/conversation）
  messages    — 会话历史消息

优势 vs JSON:
  1. 按关键词、类型、时间、重要性查询
  2. 智能召回（不 dump 全部到 prompt）
  3. 去重/矛盾检测在 SQL 层
  4. 事务安全
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_DB_PATH = settings.agent_data_dir / "memory.db"
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """每个线程一个连接。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(_DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


def init_db() -> None:
    """建表。幂等。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type TEXT    NOT NULL,  -- stable_profile | episodic | task | task_todos | task_plan | conversation
            content     TEXT    NOT NULL,
            tags        TEXT    DEFAULT '[]',  -- JSON array
            importance  INTEGER DEFAULT 0,     -- 0-10
            source      TEXT    DEFAULT '',
            session_id  TEXT    DEFAULT '',
            superseded_by TEXT  DEFAULT '',
            deprecated  INTEGER DEFAULT 0,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memories_type
            ON memories(memory_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_tags
            ON memories(tags);
        CREATE INDEX IF NOT EXISTS idx_memories_deprecated
            ON memories(deprecated);

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL,
            role        TEXT    NOT NULL,  -- human | ai
            content     TEXT    NOT NULL,
            created_at  TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, created_at);

        -- FTS for full-text search on memories
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(content, memory_type, tokenize='unicode61');
    """)
    conn.commit()
    _rebuild_fts(conn)


def _rebuild_fts(conn: sqlite3.Connection | None = None) -> None:
    """重建 FTS 索引。"""
    c = conn or _get_conn()
    try:
        c.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        c.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def add_memory(
    content: str,
    memory_type: str,
    tags: list[str] | None = None,
    importance: int = 0,
    source: str = "",
    session_id: str = "",
) -> int:
    """新增一条记忆，自动检测矛盾并降级旧记忆。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    tags_json = json.dumps(tags or [], ensure_ascii=False)

    # 矛盾检测：找内容相似的反向旧记忆
    _mark_conflicting(conn, content, memory_type, tags or [])

    cursor = conn.execute(
        """INSERT INTO memories (memory_type, content, tags, importance, source,
                                 session_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (memory_type, content, tags_json, importance, source, session_id, now, now),
    )
    row_id = cursor.lastrowid
    try:
        conn.execute(
            "INSERT INTO memories_fts(rowid, content, memory_type) VALUES (?, ?, ?)",
            (row_id, content, memory_type),
        )
    except Exception:
        pass
    conn.commit()
    return row_id


def _mark_conflicting(conn: sqlite3.Connection, content: str,
                      memory_type: str, tags: list[str]) -> None:
    """检测新旧矛盾。"""
    import re

    conflict_pairs = [
        (re.compile(r"(不?吃)"), "吃", "不吃"),
        (re.compile(r"(不?喜欢)"), "喜欢", "不喜欢"),
        (re.compile(r"(讨厌)"), "讨厌", "不讨厌"),
        (re.compile(r"(错误|不对|更正|记错|不是|更新)"), None, None),
    ]

    old_rows = conn.execute(
        "SELECT id, content, tags FROM memories WHERE memory_type=? AND deprecated=0 ORDER BY created_at DESC LIMIT 20",
        (memory_type,),
    ).fetchall()

    for old in old_rows:
        old_content = old["content"]
        old_tags = json.loads(old["tags"]) if old["tags"] else []
        if "deprecated" in old_tags:
            continue

        for pattern, pos_word, neg_word in conflict_pairs:
            if pos_word and neg_word:
                if pos_word in old_content and neg_word in content:
                    conn.execute(
                        "UPDATE memories SET deprecated=1, superseded_by=? WHERE id=?",
                        (content[:80], old["id"]),
                    )
                    break
                if neg_word in old_content and pos_word in content:
                    conn.execute(
                        "UPDATE memories SET deprecated=1, superseded_by=? WHERE id=?",
                        (content[:80], old["id"]),
                    )
                    break
            else:
                if pattern.search(old_content) and pattern.search(content):
                    old_kw = set(old_content.replace("不", "").split()[:5])
                    new_kw = set(content.replace("不", "").split()[:5])
                    if old_kw & new_kw:
                        conn.execute(
                            "UPDATE memories SET deprecated=1, superseded_by=? WHERE id=?",
                            (content[:80], old["id"]),
                        )
                        break

    # 主题相似但取值变化（如'健身晚上'→'健身早上'）：同一核心名词 + 值变化
    _VALUE_PAIRS = [
        ("晚上", "早上"), ("早上", "晚上"), ("白天", "晚上"), ("晚上", "白天"),
        ("以前", "现在"), ("现在", "以前"), ("旧", "新"), ("新", "旧"),
        ("不", ""), ("", "不"),
    ]

    for old in old_rows:
        old_content = old["content"]
        old_tags = json.loads(old["tags"]) if old["tags"] else []
        if "deprecated" in old_tags:
            continue

        # 主题关键词提取：2-4 字的中文名词（排除值词/虚词）
        def _topic_keywords(text: str) -> set[str]:
            import re
            cands = re.findall(r"[一-鿿]{2,4}", text)
            stop = {"用户", "时间", "之前", "现在", "以后", "已经", "这个", "那个",
                    "没有", "不是", "进行", "每天", "晚上", "早上", "改为", "改成",
                    "从晚", "到早", "需要", "一条", "开始", "记录", "以前", "目前",
                    "其他", "以及", "然后", "如果", "因为", "所以", "但是", "而且",
                    "还是", "或者", "除了", "关于", "对于", "这样", "那样"}
            return {w for w in cands if w not in stop}

        old_kw = _topic_keywords(old_content)
        new_kw = _topic_keywords(content)
        shared = old_kw & new_kw
        if not shared:
            # 宽松：共享任何 2 字子串（'健身' 与 '健身时间'）
            for ok in old_kw:
                for nk in new_kw:
                    if ok[:2] == nk[:2]:
                        shared.add(ok)
            if not shared:
                continue

        # 值变化检测：同一主题下出现冲突词对（一个在旧、一个在新）
        old_flat = old_content
        new_flat = content
        value_conflict = False
        for v1, v2 in _VALUE_PAIRS:
            if v1 and v2:
                if v1 in old_flat and v2 in new_flat:
                    value_conflict = True
                    break
                if v2 in old_flat and v1 in new_flat:
                    value_conflict = True
                    break
        if value_conflict:
            conn.execute(
                "UPDATE memories SET deprecated=1, superseded_by=? WHERE id=?",
                (content[:80], old["id"]),
            )


def update_profile(data: dict[str, Any]) -> None:
    """更新/合并 profile。直接用 JSON blob 存成一条 memory。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    content = json.dumps(data, ensure_ascii=False)

    existing = conn.execute(
        "SELECT id FROM memories WHERE memory_type='stable_profile' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE memories SET content=?, updated_at=? WHERE id=?",
            (content, now, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO memories (memory_type, content, tags, importance, source, created_at, updated_at)
               VALUES ('stable_profile', ?, '[]', 10, 'system', ?, ?)""",
            (content, now, now),
        )
    conn.commit()


def save_message(session_id: str, role: str, content: str) -> int:
    """保存一条会话消息。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    cursor = conn.execute(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, now),
    )
    conn.commit()
    return cursor.lastrowid


def save_task_todos(todos: list[dict]) -> None:
    """保存当前待办列表（清旧写新）。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    # 软删除旧的 task todos
    conn.execute(
        "UPDATE memories SET deprecated=1, superseded_by=? WHERE memory_type='task_todos' AND deprecated=0",
        (f"updated_at {now}",),
    )
    for t in todos:
        conn.execute(
            """INSERT INTO memories (memory_type, content, tags, importance, source, created_at, updated_at)
               VALUES ('task_todos', ?, '[]', ?, 'task_op', ?, ?)""",
            (json.dumps(t, ensure_ascii=False), 5, now, now),
        )
    conn.commit()


def save_plan_history(plan_id: str, date: str, summary: str,
                      items: list[dict]) -> None:
    """保存一条计划历史。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    content = json.dumps({"plan_id": plan_id, "summary": summary, "items": items},
                         ensure_ascii=False)
    conn.execute(
        """INSERT INTO memories (memory_type, content, tags, importance, source, session_id, created_at, updated_at)
           VALUES ('task_plan', ?, '["plan"]', 6, 'plan', ?, ?, ?)""",
        (content, date, now, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_profile() -> dict[str, Any]:
    """获取最新的 profile 数据。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT content FROM memories WHERE memory_type='stable_profile' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        return {}


def search_memories(
    query: str = "",
    memory_type: str | None = None,
    limit: int = 10,
    min_importance: int = 0,
) -> list[dict[str, Any]]:
    """搜索记忆 — 核心查询接口。

    支持：
    - 全表关键词搜索（FTS5）
    - 按类型过滤
    - 按重要性筛选
    - 排除 deprecated 条目
    """
    conn = _get_conn()

    if query.strip():
        # FTS5 搜索
        safe_query = query.replace('"', '""').replace("'", "''")
        sql = """
            SELECT m.id, m.memory_type, m.content, m.tags, m.importance,
                   m.source, m.session_id, m.created_at, m.updated_at
            FROM memories_fts f
            JOIN memories m ON f.rowid = m.id
            WHERE memories_fts MATCH ?
              AND m.deprecated = 0
        """
        params: list[Any] = [safe_query]

        if memory_type:
            sql += " AND m.memory_type = ?"
            params.append(memory_type)
        if min_importance > 0:
            sql += " AND m.importance >= ?"
            params.append(min_importance)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
    else:
        sql = """
            SELECT * FROM memories
            WHERE deprecated = 0
        """
        params = []
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if min_importance > 0:
            sql += " AND importance >= ?"
            params.append(min_importance)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_recent_memories(memory_type: str, limit: int = 10) -> list[dict[str, Any]]:
    """获取最近的记忆（按时间倒序）。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM memories
           WHERE memory_type=? AND deprecated=0
           ORDER BY created_at DESC LIMIT ?""",
        (memory_type, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_important_memories(memory_type: str, min_importance: int = 3,
                           limit: int = 10) -> list[dict[str, Any]]:
    """获取重要的记忆（按重要性 + 时间）。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM memories
           WHERE memory_type=? AND deprecated=0 AND importance >= ?
           ORDER BY importance DESC, created_at DESC LIMIT ?""",
        (memory_type, min_importance, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_todos() -> list[dict[str, Any]]:
    """获取当前 todos。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT content FROM memories
           WHERE memory_type='task_todos' AND deprecated=0
           ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()
    todos = []
    for r in rows:
        try:
            t = json.loads(r["content"])
            if isinstance(t, dict) and t.get("title"):
                todos.append(t)
        except (json.JSONDecodeError, TypeError):
            pass
    # 去重
    seen = set()
    deduped = []
    for t in todos:
        title = t.get("title", "").strip()
        if title and title not in seen:
            seen.add(title)
            deduped.append(t)
    return deduped


def get_plan_history(limit: int = 5) -> list[dict[str, Any]]:
    """获取最近的有效计划历史。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT content FROM memories
           WHERE memory_type='task_plan' AND deprecated=0
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    plans = []
    for r in rows:
        try:
            data = json.loads(r["content"])
            plans.append(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return plans


def get_session_messages(session_id: str, limit: int = 20) -> list[dict[str, str]]:
    """获取会话消息。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT role, content, created_at FROM messages
           WHERE session_id=?
           ORDER BY created_at ASC LIMIT ?""",
        (session_id, limit * 2),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


# ---------------------------------------------------------------------------
# 智能上下文构建 v2 — 分层 + 按需检索
# ---------------------------------------------------------------------------


def _intent_classify(message: str) -> tuple[str, list[str]]:
    """意图分类 + 实体提取。

    Returns:
        ("chatbot" | "task" | "project" | "plan" | "person" | "knowledge" | "memory" | "default",
         ["秋招", "RAG", ...])
    """
    text = message.lower()
    entities: list[str] = []

    # 实体提取（简单关键词匹配）
    project_kw = ["秋招", "招聘", "offer", "简历", "面试", "实习", "工作"]
    person_kw = ["我", "塔塔", "女朋友", "小曾", "背景", "名字"]
    tech_kw = ["rag", "笔记", "知识", "架构", "代码", "部署", "算法", "数据库"]
    task_kw = ["todo", "待办", "任务", "计划", "添加", "删除", "改", "生成"]
    memory_kw = ["记忆", "记得", "记住", "之前", "上次", "说过"]

    text_lower = text.lower()
    for kw_list, label in [
        (project_kw, "project"),
        (person_kw, "person"),
        (tech_kw, "knowledge"),
        (task_kw, "task"),
        (memory_kw, "memory"),
    ]:
        for kw in kw_list:
            if kw in text_lower:
                entities.append(kw)
                break

    # 意图
    if any(kw in text_lower for kw in ["计划", "安排", "明天", "today"]):
        return "plan", entities
    if any(kw in text_lower for kw in ["反思", "复盘", "分析", "评价"]):
        return "reflect", entities
    if any(kw in text_lower for kw in ["记忆", "记住", "忘了"]):
        return "memory", entities
    if any(kw in text_lower for kw in ["添加", "删除", "新建", "改成"]):
        return "task", entities
    if any(kw in text_lower for kw in ["搜", "查", "找", "什么", "如何", "怎么"]):
        return "knowledge", entities

    return "chatbot", entities


def _load_session_block(session_id: str | None,
                        message: str,
                        max_chars: int = 1500) -> str:
    """加载会话层：摘要 + 最近 N 轮。"""
    if not session_id:
        return ""

    from .session import load_messages
    msgs = load_messages(session_id, max_turns=10)
    if not msgs:
        return ""

    lines = []
    recent_turns = msgs[-6:]  # 最近 3 轮对话

    for m in recent_turns:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "human":
            lines.append(f"用户: {content[:300]}")
        elif role == "ai":
            lines.append(f"助手: {content[:200]}")
    return ("## 最近对话\n" + "\n".join(lines)) if lines else ""


def _load_carryover_block(session_id: str | None) -> str:
    """加载跨会话延续（JSONL resume）。"""
    if not session_id:
        return ""
    try:
        from .session_jsonl import get_carryover_context
        return get_carryover_context(session_id)
    except Exception:
        return ""


def _load_intent_block(intent: str, entities: list[str],
                       message: str, budget: int) -> list[tuple[str, str, int]]:
    """按需检索 — 根据意图和实体选择 topic + vault 内容。

    Returns:
        [(section_name, text, max_chars), ...]
    """
    sections: list[tuple[str, str, int]] = []
    remaining = budget

    if remaining <= 0:
        return sections

    # 按意图决定 topic 检索
    topic_paths: list[str] = []
    if intent in ("plan", "task", "project") or any(
            e in entities for e in ["秋招", "offer", "简历"]):
        topic_paths.append("projects/2027-autumn-recruitment")
    if intent == "person" or any(e in entities for e in ["我", "塔塔", "名字"]):
        topic_paths.append("people/tata")
    if intent == "knowledge" or any(e in entities for e in ["rag", "笔记", "知识"]):
        topic_paths.append("projects/personal-agent")

    # 读取 topic files
    from .topic_memory import read_topic

    processed_topics: list[str] = []
    for tp in topic_paths:
        if tp in processed_topics:
            continue
        processed_topics.append(tp)
        content = read_topic(tp)
        if content and "(Topic file not found)" not in content:
            max_c = min(len(content), remaining)
            name = tp.replace("/", "_").replace("-", "_")
            sections.append((f"topic_{name}", f"## {tp}\n{content}", max_c))
            remaining -= max_c

    # vault 检索（if budget remains）
    if remaining > 200:
        try:
            from app.obsidian import vault as vault_service
            vault_results = vault_service.search_notes(
                message, max_results=2, chars_per_match=min(remaining // 2, 1500),
            )
            if vault_results and "没有找到" not in vault_results:
                if len(vault_results) > remaining:
                    vault_results = vault_results[:remaining]
                sections.append(("vault_rag", f"## 相关笔记\n{vault_results}", len(vault_results)))
                remaining -= len(vault_results)
        except Exception:
            pass

    return sections


def build_context(task: str = "", max_tokens: int = 3500,
                  session_id: str | None = None,
                  user_id: str = "default_user") -> dict[str, Any]:
    """Context Builder v2 — 分层 + 按需检索。

    Args:
        task: 用户消息（用于 intent/entity 分析和 vault 检索）
        max_tokens: 总 token 预算
        session_id: 当前会话 ID（用于加载会话摘要）
        user_id: 用户 ID

    Layers (always):
      1. System Policy
      2. User Profile (brief)
      3. Memory Index
      4. Active Tasks
      5. Pending Approvals

    Layers (by intent):
      6. Session Summary + Recent Turns
      7. Topic Memory (matched by intent/entity)
      8. Vault Evidence (matched by intent/entity)
    """
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    char_budget = max_tokens * 2
    used_chars = 0

    def _add(name: str, text: str, max_c: int) -> None:
        nonlocal used_chars
        if not text:
            return
        rem = char_budget - used_chars
        if len(text) > max_c:
            text = text[:max_c] + "\n...(truncated)"
        if len(text) > rem:
            text = text[:rem]
        if len(text) > 30:
            parts.append(text)
            sources.append({"layer": "agent_data", "type": name})
            used_chars += len(text)

    # ── Layer 1: System Policy ──
    try:
        policy_path = settings.agent_data_dir / "memory" / "system_policy.md"
        if policy_path.exists():
            text = policy_path.read_text(encoding="utf-8")
            _add("system_policy", "## 系统策略\n" + text[:1200], 1300)
    except Exception:
        pass

    # ── Layer 2: User Profile ──
    profile = get_profile()
    if profile:
        lines = [f"- {k}: {v}" for k, v in profile.items()
                 if not k.startswith("_") and not k.startswith("__") and v]
        if lines:
            _add("stable_profile", "## 用户画像\n" + "\n".join(lines), 400)

    # ── Layer 3: Memory Index ──
    try:
        from .topic_memory import read_index as read_memory_index
        idx = read_memory_index(max_lines=20, max_chars=600)
        if idx and len(idx) > 50:
            _add("memory_index",
                 f"## 记忆索引\n{idx}\n（read_topic_memory 读取详情）", 700)
    except Exception:
        pass

    # ── Layer 4: Active Tasks ──
    todos = get_all_todos()
    if todos:
        lines = ["待办:"]
        for t in todos[-8:]:
            title = t.get("title", "")
            desc = t.get("description", "")
            prio = t.get("priority", "medium")
            lines.append(f"  - [ ] {title} ({prio})" + (f" — {desc[:120]}" if desc else ""))
        _add("task", "\n".join(lines), 600)

    # ── Layer 5: Task Handoffs (结构化任务传递) ──
    try:
        from .handoff import get_handoff_context, get_pending_approval_context
        hf = get_handoff_context()
        if hf:
            _add("handoffs", hf, 800)
        pa = get_pending_approval_context()
        if pa:
            _add("pending_approvals", pa, 500)
    except Exception:
        pass

    # ── Layer 6: Session carryover (JSONL resume) ──
    carryover = _load_carryover_block(session_id or "")
    if carryover:
        _add("session_carryover", carryover, 800)

    # ── Layer 7+8: By intent — topic memory + vault ──
    if task:
        intent, entities = _intent_classify(task)
        used_before = used_chars
        intent_budget = max(0, char_budget - used_chars - 200)
        for sec_name, sec_text, sec_max in _load_intent_block(
                intent, entities, task, intent_budget):
            _add(sec_name, sec_text, sec_max)

    # ── Manifest ──
    manifest = {
        "context_tokens_est": used_chars // 2,
        "context_sources": [s["type"] for s in sources],
        "source_count": len(sources),
    }

    # ── Context Pressure Monitor: 注入压力感知 ──
    try:
        from .context_pressure import measure_pressure, format_pressure_line
        pressure = measure_pressure(session_id=session_id,
                                    context_tokens=used_chars // 2)
        manifest["pressure"] = pressure
        if pressure["level"] in ("yellow", "red"):
            _add("pressure_warning",
                 f"## 上下文压力\n{format_pressure_line(pressure)}", 200)
    except Exception:
        pass

    return {
        "context": "\n\n".join(parts),
        "sources": sources,
        "manifest": manifest,
    }


def _inject_persona(parts: list[str], sources: list[dict]) -> None:
    try:
        from app.agent.persona import load_persona, build_style_instruction
        persona = load_persona()
        if persona:
            style_ins = build_style_instruction(persona)
            if style_ins:
                parts.append(f"## 对话风格指令\n{style_ins}")
                sources.append({"layer": "agent_data", "type": "persona"})
    except Exception:
        pass


def _inject_vault_search(task: str, parts: list[str],
                         sources: list[dict], max_chars: int = 2500) -> None:
    """vault RAG 检索，带 token budget。"""
    try:
        from app.obsidian import vault as vault_service
        vault_results = vault_service.search_notes(task, max_results=2, chars_per_match=max_chars // 2)
        if vault_results and "没有找到" not in vault_results:
            if len(vault_results) > max_chars:
                vault_results = vault_results[:max_chars] + "\n...(truncated)"
            parts.append(f"## 相关笔记\n{vault_results}")
            sources.append({"layer": "vault", "type": "rag"})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 人格化输出
# ---------------------------------------------------------------------------


def format_memories_for_prompt(memories: list[dict]) -> str:
    """将记忆列表格式化为可读字符串。"""
    if not memories:
        return "(暂无)"
    lines = []
    for m in memories:
        ts = m.get("created_at", "")[:10]
        tags = json.loads(m["tags"]) if isinstance(m["tags"], str) else m.get("tags", [])
        tag_str = f" [{', '.join(tags[:3])}]" if tags else ""
        lines.append(f"- [{ts}]{tag_str} [{m['memory_type']}] {m['content'][:300]}")
    return "\n".join(lines)


def format_conversation_messages(messages: list[dict]) -> str:
    """格式化会话为可读字符串。"""
    if not messages:
        return "(暂无对话历史)"
    lines = []
    for m in messages:
        role = m.get("role", "?")
        if role == "human":
            lines.append(f"用户: {m.get('content', '')[:300]}")
        elif role == "ai":
            lines.append(f"助手: {m.get('content', '')[:300]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 初始化 & 迁移
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def migrate_from_json() -> dict[str, int]:
    """从 JSON 文件迁移数据到 SQLite。返回统计。"""
    from app.agent.agent_data_service import (
        read_memory as json_read,
    )

    counts: dict[str, int] = {"memories": 0, "messages": 0}
    conn = _get_conn()

    # 1. 迁移 stable_profile
    profile = json_read("stable_profile")
    if profile:
        update_profile(profile)
        counts["memories"] += 1

    # 2. 迁移 episodic
    epi = json_read("episodic")
    for entry in epi.get("entries", []):
        tags = entry.get("tags", [])
        importance = 3 if "deprecated" not in tags else 0
        if "deprecated" in tags:
            continue
        add_memory(
            content=entry.get("content", ""),
            memory_type="episodic",
            tags=tags,
            importance=importance,
            source="migrated",
        )
        counts["memories"] += 1

    # 3. 迁移 task_memory
    task = json_read("task")
    for h in task.get("history", []):
        plan_id = h.get("plan_id", "")
        items = h.get("items", [])
        summary = h.get("summary", "")
        if plan_id.startswith("taskop"):
            # task 操作记录
            add_memory(
                content=f"任务操作: {summary}",
                memory_type="task",
                tags=["task_op", f"session_{plan_id[-8:]}"],
                importance=2,
                source="task_op",
            )
        else:
            # 计划记录
            save_plan_history(plan_id, h.get("date", ""), summary, items)
        counts["memories"] += 1

    # 4. 迁移 todos
    todos = task.get("todos", [])
    if todos:
        save_task_todos(todos)
        counts["memories"] += len(todos)

    # 5. 迁移 session messages
    sess_dir = settings.agent_data_dir / "sessions"
    if sess_dir.exists():
        for sess_folder in sorted(sess_dir.iterdir()):
            if sess_folder.is_dir():
                msg_file = sess_folder / "messages.json"
                if msg_file.exists():
                    try:
                        msgs = json.loads(msg_file.read_text(encoding="utf-8"))
                        for m in msgs:
                            save_message(
                                session_id=sess_folder.name,
                                role=m.get("role", "human"),
                                content=m.get("content", ""),
                            )
                            counts["messages"] += 1
                    except Exception:
                        pass

    conn.commit()
    return counts


def get_stats() -> dict[str, int]:
    """获取记忆存储统计。"""
    conn = _get_conn()
    stats: dict[str, int] = {}
    rows = conn.execute(
        "SELECT memory_type, COUNT(*) as cnt FROM memories WHERE deprecated=0 GROUP BY memory_type"
    ).fetchall()
    for r in rows:
        stats[r["memory_type"]] = r["cnt"]
    stats["total_memories"] = sum(stats.values())
    msg_count = conn.execute("SELECT COUNT(*) as cnt FROM messages").fetchone()
    stats["total_messages"] = msg_count["cnt"] if msg_count else 0
    return stats
