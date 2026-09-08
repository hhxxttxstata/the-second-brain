# -*- coding: utf-8 -*-
"""记忆可信度防护回归测试。

覆盖:
  1. trace 滚动清理写审计日志(cleanup_log.jsonl)
  2. evolution state.json 原子写(无 .tmp 残留、内容一致)
  3. MEMORY.md 悬空索引巡检(删悬空行、保留有效行)
  4. 蒸馏 provenance 只引用真实存在的 trace
"""
from __future__ import annotations

import json

from app.agent import trace as tr
from app.agent.evolution import experience as exp
from app.agent.evolution.distill import _apply_experiences
from app.agent.topic_memory import prune_dangling_index_entries


def test_trim_trace_dir_writes_cleanup_log(monkeypatch, tmp_path):
    """超过 MAX_TRACES 的旧 trace 被删除时,cleanup_log.jsonl 记录被删 id。"""
    monkeypatch.setattr(tr, "_TRACE_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    # 造 MAX_TRACES + 2 个 trace 文件,旧文件 mtime 更早
    import os
    for i in range(tr.MAX_TRACES + 2):
        f = tmp_path / f"trace_old{i:03d}.json"
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (1000 + i, 1000 + i))
    tr._trim_trace_dir()
    assert len(list(tmp_path.glob("trace_*.json"))) == tr.MAX_TRACES
    log = tmp_path / "cleanup_log.jsonl"
    assert log.exists(), "清理未留审计日志"
    entries = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    removed = [tid for e in entries for tid in e["removed"]]
    assert removed == ["trace_old000", "trace_old001"]
    assert entries[0]["reason"].startswith("trim>")


def test_save_state_atomic_write(monkeypatch, tmp_path):
    """state.json 原子写:落盘内容一致、无 .tmp 残留。"""
    monkeypatch.setattr(exp, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(exp, "_STATE_PATH", tmp_path / "state.json")
    exp.save_state({"distill_count": 1, "last_distilled_at": "2026-09-08T00:00:00"})
    assert (tmp_path / "state.json").exists()
    assert not (tmp_path / "state.json.tmp").exists()
    assert exp.load_state()["distill_count"] == 1
    # 并发写不崩(锁生效)
    import threading
    threads = [threading.Thread(target=exp.save_state, args=({"distill_count": i},))
               for i in range(2, 6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert exp.load_state()["distill_count"] in (2, 3, 4, 5)  # 文件始终完整可解析


def test_prune_dangling_index_entries(monkeypatch, tmp_path):
    """巡检删除指向不存在文件的指针行,保留有效指针与普通文本。"""
    memdir = tmp_path / "memory"
    memdir.mkdir()
    (memdir / "people").mkdir()
    (memdir / "people" / "tata.md").write_text("real", encoding="utf-8")
    index = memdir / "MEMORY.md"
    index.write_text(
        "# Memory Index\n\n"
        "## People\n"
        "- 塔塔: 见 people/tata.md | 有效\n"
        "- 幻觉: 见 people/xiaozeng.md | 悬空\n\n"
        "## Reserved\n"
        "- System Policy: 见 system_policy.md | 也是悬空\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.agent.topic_memory._MEMORY_DIR", memdir)
    monkeypatch.setattr("app.agent.topic_memory._INDEX_FILE", index)

    removed = prune_dangling_index_entries()

    assert len(removed) == 2
    text = index.read_text(encoding="utf-8")
    assert "塔塔" in text and "xiaozeng" not in text and "system_policy" not in text


def test_distill_provenance_filters_missing_traces(monkeypatch, tmp_path):
    """蒸馏落库时,provenance 不引用已不存在的 trace 文件。"""
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    (traces_dir / "trace_exists.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(exp, "_TRACES_DIR", traces_dir)

    # 隔离 topic memory 写入(本测试只关心 provenance 文本构造)
    calls: list[tuple] = []

    def fake_write_topic(category, content, append=False):
        calls.append((category, content))

    monkeypatch.setattr("app.agent.topic_memory.write_topic", fake_write_topic)
    monkeypatch.setattr("app.agent.agent_data_service.add_episodic", lambda *a, **k: None)
    monkeypatch.setattr("app.agent.topic_memory.upsert_index_entry", lambda *a, **k: None)

    _apply_experiences(
        [{"title": "t", "content": "c", "category": "lessons", "tags": []}],
        batch_id="distill_test",
        trace_ids=["trace_exists", "trace_gone", "trace_also_gone"],
    )

    _, content = calls[0]
    assert "trace_exists" in content
    assert "trace_gone" not in content and "trace_also_gone" not in content
    assert "batch=distill_test" in content
