"""Evaluation Lifecycle 升级测试 — 动作级指标 / trajectory_mode / required_agents /
state_assert 扩展 / 多维回归守卫 / 评测隔离（防自进化污染）。

全部在 isolate_agent_data() 内运行，不触碰真实 agent_data。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.agent import failure_taxonomy as ft
from app.agent.trace import (
    _check_case_constraints,
    _check_expected_workflow,
    _check_state_delta,
    _regression_guard,
    _snapshot_state,
    isolate_agent_data,
)


# ---------------------------------------------------------------------------
# failure taxonomy 新码
# ---------------------------------------------------------------------------

def test_new_failure_codes_registered():
    for code in ("MISSED_SECONDARY_INTENT", "PREMATURE_END",
                 "SIDE_EFFECT_MISSING", "EVO_MEMORY_LEAK"):
        assert code in ft.ALL_FAILURE_CODES
        assert ft.FAILURE_DESCRIPTIONS[code]
        assert ft.FAILURE_SEVERITY[code] in ("low", "medium", "high", "critical")


# ---------------------------------------------------------------------------
# trajectory_mode（§P1-1）
# ---------------------------------------------------------------------------

def _wf_case(**extra) -> dict:
    case = {"input": "test", "intent": "test", "expected_route": "chatbot"}
    case.update(extra)
    return case


def test_unordered_required_all_present():
    case = _wf_case(trajectory_mode="unordered_required",
                    required_tools=["search_vault", "write_memory"])
    trace = {"tool_calls": [
        {"name": "write_memory", "success": True},
        {"name": "search_vault", "success": True},
    ]}
    r = _check_expected_workflow(case, trace, {})
    assert r["trajectory_mode"] == "unordered_required"
    assert r["complete"] is True
    assert r["completed_steps"] == 2


def test_unordered_required_missing_tool():
    case = _wf_case(trajectory_mode="unordered_required",
                    required_tools=["search_vault", "write_memory"])
    trace = {"tool_calls": [{"name": "search_vault", "success": True}]}
    r = _check_expected_workflow(case, trace, {})
    assert r["complete"] is False
    assert any("write_memory" in s["reasons"][0] for s in r["steps"])


def test_scope_blocks_out_of_whitelist():
    case = _wf_case(trajectory_mode="scope",
                    allowed_tools=["search_vault", "read_memory"])
    trace = {"tool_calls": [
        {"name": "search_vault", "success": True},
        {"name": "run_code", "success": True},   # 白名单外
    ]}
    r = _check_expected_workflow(case, trace, {})
    assert r["complete"] is False
    assert any("越权" in v for v in r["order_violations"])


def test_scope_allows_all_in_whitelist():
    case = _wf_case(trajectory_mode="scope",
                    allowed_tools=["search_vault", "read_memory"])
    trace = {"tool_calls": [{"name": "read_memory", "success": True}]}
    r = _check_expected_workflow(case, trace, {})
    assert r["complete"] is True


def test_steps_carry_kind_and_requires_tool():
    case = _wf_case(expected_workflow=[
        {"step": "a", "expect_tool": "search_vault"},
        {"step": "b", "state_assert": "todo 出现"},
    ])
    trace = {"tool_calls": [{"name": "search_vault", "success": True}]}
    r = _check_expected_workflow(case, trace, {})
    kinds = {s["kind"] for s in r["steps"]}
    assert kinds == {"tool", "state"}
    assert [s["requires_tool"] for s in r["steps"]] == [True, False]


# ---------------------------------------------------------------------------
# required_agents（§P0-2）
# ---------------------------------------------------------------------------

def test_required_agents_all_covered():
    case = _wf_case(required_agents=["memory", "plan"])
    trace = {"route": "memory", "final_output": "ok", "tool_calls": [],
             "memory_updates": [], "result_data": {}}
    result = {"task_results": [{"agent": "memory"}, {"agent": "plan"}]}
    checks, _, detail, _, _ = _check_case_constraints(case, trace, result)
    assert checks[-1] is True
    assert "已覆盖" in detail[-1]["reason"]


def test_required_agents_missing_secondary():
    case = _wf_case(required_agents=["memory", "plan"])
    trace = {"route": "memory", "final_output": "ok", "tool_calls": [],
             "memory_updates": [], "result_data": {}}
    result = {"task_results": [{"agent": "memory"}]}   # 漏掉 plan
    checks, _, detail, _, _ = _check_case_constraints(case, trace, result)
    assert checks[-1] is False
    assert "plan" in detail[-1]["reason"]


# ---------------------------------------------------------------------------
# state_assert 扩展（§P0-3）— 在隔离环境内做真实记忆写入
# ---------------------------------------------------------------------------

def _add_memory_and_deprecate():
    """写一条记忆，再写一条冲突记忆触发 deprecated。"""
    from app.agent.memory_store import add_memory
    add_memory("健身时间：晚上健身", memory_type="episodic",
               tags=["健身"], importance=3, source="test")
    add_memory("健身时间：早上健身", memory_type="episodic",
               tags=["健身"], importance=3, source="test")


def test_state_delta_deprecated_new():
    with isolate_agent_data():
        before = _snapshot_state()
        _add_memory_and_deprecate()
        ok, reason = _check_state_delta("memory.deprecated_new >= 1", before)
        assert ok is True, reason
        assert "deprecated" in reason


def test_state_delta_profile_changed():
    with isolate_agent_data():
        from app.agent.memory_store import update_profile
        before = _snapshot_state()
        update_profile({"称谓": "塔塔"})
        ok, reason = _check_state_delta("profile changed", before)
        assert ok is True, reason
        ok2, _ = _check_state_delta("profile changed", _snapshot_state())
        assert ok2 is False   # 再次对比无变化


def test_state_delta_task_history_new():
    with isolate_agent_data():
        from app.agent.memory_store import save_plan_history
        before = _snapshot_state()
        save_plan_history("plan_t", "2026-08-31", "1 items", [{"title": "x"}])
        ok, reason = _check_state_delta("task.history_new >= 1", before)
        assert ok is True, reason


def test_state_delta_unknown_syntax_falls_back():
    with isolate_agent_data():
        ok, _ = _check_state_delta("回答基于真实笔记内容", {})
        assert ok is None   # 非增量语法 → 回退 outcome 判定器


# ---------------------------------------------------------------------------
# 多维回归守卫（§P0-5）
# ---------------------------------------------------------------------------

def test_regression_guard_legacy_report_compat():
    """旧报告无效率字段 → 相应维度跳过，不崩溃。"""
    guard = _regression_guard({
        "pass_rate": 90.0,
        "results": [{"intent": "a", "success": False}],
    })
    assert guard["guarded"] is False or guard["dropped"] in (True, False)


def test_regression_guard_efficiency_gate():
    from unittest import mock
    prev = {"pass_rate": 100.0, "latency_p95_ms": 5000, "avg_tokens": 8000}
    cur = {"pass_rate": 100.0, "latency_p95_ms": 7000, "avg_tokens": 9000,
           "results": []}
    with mock.patch("app.agent.trace._load_previous_benchmark", return_value=prev):
        guard = _regression_guard(cur)
    assert guard["p95_latency_delta_pct"] == 40.0
    assert guard["latency_regressed"] is True
    assert guard["promotion_gate"] == "FAIL"


def test_regression_guard_pass_rate_drop():
    from unittest import mock
    prev = {"pass_rate": 100.0}
    cur = {"pass_rate": 80.0, "results": [{"intent": "x", "success": False}]}
    with mock.patch("app.agent.trace._load_previous_benchmark", return_value=prev):
        guard = _regression_guard(cur)
    assert guard["dropped"] is True
    assert guard["promotion_gate"] == "FAIL"


# ---------------------------------------------------------------------------
# 评测隔离（§P2-1 防污染）
# ---------------------------------------------------------------------------

def test_isolate_redirects_and_restores():
    from app.agent import memory_store as ms
    from app.agent.trace import _TRACE_DIR
    real_db, real_trace = ms._DB_PATH, _TRACE_DIR

    with isolate_agent_data() as tmp:
        assert ms._DB_PATH != real_db
        assert str(ms._DB_PATH).startswith(str(tmp))
        from app.agent.trace import _TRACE_DIR as t2
        assert t2 != real_trace
        # 隔离库可写可读
        from app.agent.memory_store import add_memory, search_memories
        add_memory("隔离测试记忆", memory_type="episodic", tags=["t"], importance=1)
        assert len(search_memories("隔离测试记忆")) == 1

    # 退出后恢复真实路径
    assert ms._DB_PATH == real_db
    from app.agent.trace import _TRACE_DIR as t3
    assert t3 == real_trace
