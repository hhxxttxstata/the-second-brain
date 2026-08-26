"""遗留问题修复回归测试 (2026-08)。

覆盖:
  1. carryover: 夹具文件 (session.jsonl / benchmark_fixture_session) 不再注入,
     真实日期会话文件按时间倒序注入, 排除当前会话
  2. handoff 生命周期: create_handoff → 注入 build_context → complete_handoff
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# 1. session_logs / carryover 修复
# ---------------------------------------------------------------------------


def test_carryover_skips_fixture_and_excludes_current(monkeypatch, tmp_path):
    import os

    from app.agent import session_jsonl as sj

    # 隔离日志目录: 一个夹具文件 + 两个真实日期文件
    log_dir = tmp_path / "session_logs"
    log_dir.mkdir()
    fixture = log_dir / "session.jsonl"  # benchmark 夹具 (字符串序最大, 旧 bug 会优先读它)
    fixture.write_text(json.dumps({
        "session_id": "benchmark_fixture_session", "goal": "夹具内容", "completed": ["x"],
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    now = datetime.now()
    for i, day in enumerate((now - timedelta(days=2), now - timedelta(days=1))):
        f = log_dir / f"{day.strftime('%Y-%m-%d')}.jsonl"
        f.write_text(json.dumps({
            "session_id": f"session_{day.strftime('%Y-%m-%d')}",
            "goal": f"真实会话{i}", "completed": [f"真实完成{i}"],
            "decisions": [f"真实决策{i}"],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        # 显式设置 mtime, 保证较新的文件排在前面
        os.utime(f, (now.timestamp() - (1 - i) * 3600, now.timestamp() - (1 - i) * 3600))

    monkeypatch.setattr(sj, "_SESSION_LOG_DIR", log_dir)

    out = sj.get_carryover_context("session_2026-08-17")  # 当前会话
    assert "夹具" not in out
    assert "benchmark" not in out
    assert "真实会话" in out
    assert "真实决策" in out
    assert "session_2026-08-17" not in out  # 当前会话被排除

    # 真实文件按 mtime 倒序: 最近一天 (真实会话1) 排前面
    out2 = sj.get_carryover_context("none")
    assert out2.index("真实会话1") < out2.index("真实会话0")


# ---------------------------------------------------------------------------
# 2. handoff 生命周期
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_handoff(monkeypatch, tmp_path):
    """把 handoff 的 tasks/ + handoffs/ 重定向到临时目录。"""
    from app.agent import handoff as hf

    tasks_dir = tmp_path / "tasks"
    handoffs_dir = tmp_path / "handoffs"
    monkeypatch.setattr(hf, "_TASKS_DIR", tasks_dir)
    monkeypatch.setattr(hf, "_HANDOFFS_DIR", handoffs_dir)
    return hf


def test_handoff_lifecycle(monkeypatch, tmp_path, isolated_handoff, mock_llm, isolated_data):
    """create → active → 注入 context → complete → 移除。"""
    from app.agent.handoff import (
        complete_handoff, create_handoff, get_active_handoffs, get_handoff_context,
    )
    from app.agent.agent_data_service import build_context

    rec = create_handoff(
        goal="整理秋招面试八股并写入笔记",
        pending_tool="vault_append",
        pending_params={"rel_path": "notes/秋招准备/面试八股.md"},
        completed=["已收集 10 道题"],
        next_step="把剩余 5 道题追加进面试八股.md",
    )
    task_id = rec["task_id"]
    assert task_id.startswith("task_")

    # 活跃列表与上下文注入
    active = get_active_handoffs()
    assert any(h["task_id"] == task_id for h in active)
    ctx_text = get_handoff_context()
    assert "整理秋招面试八股" in ctx_text

    # build_context 的 Layer 5 注入 (使用隔离的 memory.db / checkpoint)
    ctx = build_context(task="继续秋招任务", session_id="reg_handoff_ctx")
    assert "活跃任务" in ctx["context"] or "秋招" in ctx["context"]

    # 完成 → 从活跃列表移除
    assert complete_handoff(task_id, result="已全部写入") is True
    active = get_active_handoffs()
    assert all(h["task_id"] != task_id for h in active)
    # 幂等: 重复完成返回 False
    assert complete_handoff(task_id) is False
