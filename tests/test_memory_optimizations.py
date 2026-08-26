"""记忆-上下文优化回归测试 (2026-08)。

覆盖:
  P0-1 画像条目化: v1 迁移 / 变更检测 / 历史保留 / 无变更不写
  P0-2 ask_clarification 工具注册与调用
  P0-3 delete_memory 工具: 两段式 (搜索候选 → 按 id 删除)
  P2-A recency 衰减: 召回更新 last_accessed_at, 排序沉底旧记忆
  P2-B 关联一跳: MEMORY.md 关联字段解析与召回展开
  P2-C 时间锚: build_context 注入今天日期
  P2-D 计划操作简化: TASK_OPS_PROMPT 不再支持 merge/split
  P1-C trace 滚动: 超过上限清理最旧
"""
from __future__ import annotations

import json
from datetime import date

import pytest


# ---------------------------------------------------------------------------
# P0-1 画像条目化
# ---------------------------------------------------------------------------


def test_profile_entries_migration_and_history(mock_llm, isolated_data):
    import app.agent.memory_store as ms

    # v1 旧格式数据
    conn = ms._get_conn()
    conn.execute(
        "INSERT INTO memories (memory_type, content, tags, importance, source, created_at, updated_at) "
        "VALUES ('stable_profile', ?, '[]', 10, 'system', ?, ?)",
        ('{"称谓": "塔塔", "城市": "南昌"}', "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
    )
    conn.commit()

    assert ms.get_profile() == {"称谓": "塔塔", "城市": "南昌"}

    r1 = ms.update_profile({"称谓": "子拓"}, source="session_test")
    assert r1["changes"] == ["称谓: 塔塔 → 子拓"]

    # 无变更不写
    r2 = ms.update_profile({"称谓": "子拓"})
    assert r2["changes"] == []

    r3 = ms.update_profile({"城市": "上海"})
    assert r3["changes"] == ["城市: 南昌 → 上海"]
    assert ms.get_profile() == {"称谓": "子拓", "城市": "上海"}

    history = ms.get_profile_history("称谓")
    assert [h["value"] for h in history] == ["子拓", "塔塔"]  # 最新在前


# ---------------------------------------------------------------------------
# P0-2 / P0-3 澄清与忘记工具
# ---------------------------------------------------------------------------


def test_clarification_and_delete_tools_registered(mock_llm, isolated_data):
    import asyncio

    from app.agent.graphs.tools import get_registry

    names = [t["name"] for t in get_registry().list_tools_for_llm()]
    assert "ask_clarification" in names
    assert "delete_memory" in names

    reg = get_registry()
    r = asyncio.run(reg.execute("ask_clarification", {"question": "你说的那个项目是指哪个？"}))
    assert r.get("success")
    assert "澄清" in r.get("result", "")


def test_delete_memory_two_phase(mock_llm, isolated_data):
    import asyncio

    from app.agent.graphs.tools import get_registry
    from app.agent.memory_store import add_memory, _get_conn

    mid = add_memory("用户每天晚上健身", memory_type="episodic", tags=["habit"])
    reg = get_registry()

    # 第一阶段：搜索候选（无 memory_id）
    r1 = asyncio.run(reg.execute("delete_memory", {"query": "健身"}))
    assert r1.get("success")
    assert f"#{mid}" in r1.get("result", "")

    # 第二阶段：按 id 删除
    r2 = asyncio.run(reg.execute("delete_memory", {"query": "健身", "memory_id": mid}))
    assert r2.get("success")
    conn = _get_conn()
    row = conn.execute("SELECT deprecated FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["deprecated"] == 1


# ---------------------------------------------------------------------------
# P2-A recency 衰减
# ---------------------------------------------------------------------------


def test_recency_tracking_on_recall(mock_llm, isolated_data):
    import app.agent.memory_store as ms

    old = ms.add_memory("三个月前的旧记忆", memory_type="episodic", importance=3)
    new = ms.add_memory("昨天的记忆", memory_type="episodic", importance=3)
    conn = ms._get_conn()
    # 模拟旧记忆很久没被访问
    conn.execute("UPDATE memories SET last_accessed_at='2026-05-01' WHERE id=?", (old,))
    conn.commit()

    # 无 query 的检索按 importance + recency 排序 → 旧记忆沉底
    rows = ms.search_memories(memory_type="episodic", limit=10)
    assert rows[0]["id"] == new

    # 召回会更新访问时间
    hit = ms.search_memories(query="旧记忆", limit=5)
    assert hit and hit[0]["id"] == old
    conn = ms._get_conn()
    row = conn.execute("SELECT access_count, last_accessed_at FROM memories WHERE id=?", (old,)).fetchone()
    assert row["access_count"] >= 1
    assert row["last_accessed_at"]


# ---------------------------------------------------------------------------
# P2-B 关联一跳
# ---------------------------------------------------------------------------


def test_related_topics_expansion(monkeypatch, tmp_path, mock_llm, isolated_data):
    from app.agent import topic_memory as tm

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    monkeypatch.setattr(tm, "_MEMORY_DIR", mem_dir)
    monkeypatch.setattr(tm, "_INDEX_FILE", mem_dir / "MEMORY.md")

    tm.upsert_index_entry("People", "塔塔", "people/tata", "用户档案",
                          related=["projects/personal-agent", "preferences"])
    assert tm.get_related_topics("people/tata") == ["projects/personal-agent", "preferences"]
    # 无关 topic 不展开
    assert tm.get_related_topics("people/other") == []

    # 旧格式（无关联字段）兼容
    tm.upsert_index_entry("Projects", "旧项目", "projects/old", "无关联")
    assert tm.get_related_topics("projects/old") == []


# ---------------------------------------------------------------------------
# P2-C 时间锚
# ---------------------------------------------------------------------------


def test_time_anchor_in_context(mock_llm, isolated_data):
    from app.agent.agent_data_service import build_context

    ctx = build_context(task="测试", session_id="reg_time")
    assert "## 时间" in ctx["context"]
    assert date.today().isoformat() in ctx["context"]


# ---------------------------------------------------------------------------
# P2-D 计划操作简化
# ---------------------------------------------------------------------------


def test_task_ops_prompt_no_merge_split(mock_llm, isolated_data):
    from app.agent.graphs.plan_graph import TASK_OPS_PROMPT

    assert '"add|delete|update|skip"' in TASK_OPS_PROMPT
    assert "merge|split" not in TASK_OPS_PROMPT


# ---------------------------------------------------------------------------
# P1-C trace 滚动
# ---------------------------------------------------------------------------


def test_trace_rolling_trim(monkeypatch, tmp_path, mock_llm, isolated_data):
    import app.agent.trace as tr

    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    monkeypatch.setattr(tr, "_TRACE_DIR", trace_dir)
    monkeypatch.setattr(tr, "MAX_TRACES", 5)

    for i in range(8):
        t = tr.TraceRecord("chatbot", f"意图{i}")
        t.success = True
        t.save()

    files = sorted(trace_dir.glob("trace_*.json"))
    assert len(files) == 5  # 只保留最近 5 条
