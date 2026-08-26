# -*- coding: utf-8 -*-
"""Failure Taxonomy 回归测试：MEMORY_RECALL_MISS 写/查意图区分（2026-08 修复）。

背景：旧判定把所有 memory 类任务（未调用读取工具）都标为 MEMORY_RECALL_MISS，
导致"记住X"这类纯写任务被误报为"存在相关记忆但未能召回"。
修复后：写记忆任务（memory_updates 非空或输出确认保存）不算召回缺失；
只有查询/回忆类任务未调用读取工具才判定。
"""
from __future__ import annotations

from app.agent.failure_taxonomy import MEMORY_RECALL_MISS, detect_failure_codes


def _trace(**overrides) -> dict:
    base = {
        "task_type": "memory",
        "tool_calls": [],
        "final_output": "",
        "error": None,
        "success": True,
        "memory_updates": [],
    }
    base.update(overrides)
    return base


def test_memory_write_with_updates_not_marked_as_recall_miss():
    """写记忆任务（memory_updates 非空）→ 不标 MEMORY_RECALL_MISS。"""
    t = _trace(
        memory_updates=[{"type": "episodic", "preview": "📝 Agent 记忆已保存 (episodic)"}],
        final_output="📝 Agent 记忆已保存 (episodic)",
    )
    assert MEMORY_RECALL_MISS not in detect_failure_codes(t)


def test_memory_graph_nonstandard_trace_decision_write_not_marked():
    """memory_graph save_trace 的非标准记录（decision=write，无其余字段）→ 不标。"""
    t = {
        "task_type": "memory",
        "decision": "write",
        "type": "episodic",
        "content": "记住：我的目标是拿到AI offer",
    }
    assert MEMORY_RECALL_MISS not in detect_failure_codes(t)


def test_memory_write_with_output_confirmation_not_marked():
    """写记忆任务（无 memory_updates 但输出确认已保存）→ 不标。"""
    t = _trace(final_output="📝 画像已更新: 手机号=13812345678")
    assert MEMORY_RECALL_MISS not in detect_failure_codes(t)


def test_memory_query_without_read_tool_marked():
    """查询/回忆类 memory 任务（无读工具、无写入证据）→ 标 MEMORY_RECALL_MISS。"""
    t = _trace(final_output="我记得你之前说过喜欢游泳")
    codes = detect_failure_codes(t)
    assert MEMORY_RECALL_MISS in codes


def test_memory_query_with_read_tool_not_marked():
    """查询类任务调用了读取工具 → 不标。"""
    t = _trace(tool_calls=[{"name": "search_memories", "success": True}])
    assert MEMORY_RECALL_MISS not in detect_failure_codes(t)


def test_other_task_types_unaffected():
    """非 memory 任务不受该判定影响。"""
    t = _trace(task_type="chatbot", final_output="你好")
    assert MEMORY_RECALL_MISS not in detect_failure_codes(t)
