# -*- coding: utf-8 -*-
"""Workflow 级评测回归测试（issue #9，合成 trace，不发真实 LLM 请求）。

覆盖四类改动：
  1. 严格判定 _judge_case — unknown outcome 不再静默放行，单独标注 suspicious_pass
  2. _check_expected_workflow — 逐步断言 + 顺序校验 + 快照增量 state_assert
  3. _compute_intent_completion / _aggregate_workflow — 多意图半失败与一级指标聚合
  4. scorecard L1B — Workflow / Multi-intent Completion Rate 从 benchmark 报告打分
"""
from __future__ import annotations

import json

import pytest

import app.agent.agent_data_service as ads
import app.agent.trace as trace_mod
from app.agent.trace import (
    _aggregate_workflow,
    _check_expected_workflow,
    _check_state_delta,
    _compute_intent_completion,
    _judge_case,
)


def _tc(*names):
    return [{"name": n, "success": True, "params": {}} for n in names]


# ---------------------------------------------------------------------------
# 1. 严格判定：unknown 不再放行
# ---------------------------------------------------------------------------

class TestJudgeCase:
    def test_unknown_no_longer_passes(self):
        # 旧规则 all(oc is not False) 会 PASS；新规则必须失败并标注 suspicious_pass
        v = _judge_case(True, [True, None], [], None)
        assert v["success"] is False
        assert v["suspicious_pass"] is True
        assert "OUTCOME_UNKNOWN" in v["failure_codes"]
        assert "OUTCOME_NOT_MET" in v["failure_codes"]

    def test_explicit_false_not_suspicious(self):
        # 明确失败是 agent 问题，不是判准盲区
        v = _judge_case(True, [True, False], [], None)
        assert v["success"] is False
        assert v["suspicious_pass"] is False
        assert "OUTCOME_UNKNOWN" not in v["failure_codes"]

    def test_all_true_passes(self):
        v = _judge_case(True, [True, True], [], None)
        assert v["success"] is True
        assert v["suspicious_pass"] is False

    def test_forbidden_fails(self):
        v = _judge_case(True, [True], ["创建重复任务"], None)
        assert v["success"] is False
        assert v["suspicious_pass"] is False

    def test_run_failure_fails(self):
        v = _judge_case(False, [True, None], [], None)
        assert v["success"] is False
        assert v["suspicious_pass"] is False

    def test_workflow_incomplete_fails(self):
        wf = {"complete": False, "completed_steps": 1, "total_steps": 2, "steps": []}
        v = _judge_case(True, [True], [], wf)
        assert v["success"] is False
        assert "WORKFLOW_INCOMPLETE" in v["failure_codes"]

    def test_workflow_complete_passes(self):
        wf = {"complete": True, "completed_steps": 2, "total_steps": 2, "steps": []}
        v = _judge_case(True, [True], [], wf)
        assert v["success"] is True


# ---------------------------------------------------------------------------
# 2. expected_workflow 逐步断言 + 顺序校验
# ---------------------------------------------------------------------------

class TestExpectedWorkflow:
    def _case(self, workflow, **kw):
        case = {"input": kw.pop("input", "测试输入"), "expected_workflow": workflow}
        case.update(kw)
        return case

    def test_ordered_steps_pass(self):
        case = self._case([
            {"step": "新增", "expect_tool": "update_task_status"},
            {"step": "更新状态", "expect_tool": "update_task_status"},
        ])
        trace = {"tool_calls": _tc("update_task_status", "update_task_status"),
                 "final_output": "done"}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["complete"] is True
        assert wf["completed_steps"] == 2
        assert wf["order_violations"] == []

    def test_order_violation_detected(self):
        # 第二步的工具没有出现在第一步之后 → 顺序违规
        case = self._case([
            {"step": "新增", "expect_tool": "update_task_status"},
            {"step": "写记忆", "expect_tool": "write_memory"},
        ])
        trace = {"tool_calls": _tc("write_memory", "update_task_status"),
                 "final_output": "done"}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["complete"] is False
        assert len(wf["order_violations"]) == 1
        assert wf["steps"][1]["ok"] is False

    def test_missing_tool_fails(self):
        case = self._case([
            {"step": "检索", "expect_tool": "search_vault"},
        ])
        trace = {"tool_calls": _tc("read_memory"), "final_output": "done"}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["steps"][0]["ok"] is False
        assert wf["complete"] is False

    def test_any_of_expect_tool(self):
        case = self._case([
            {"step": "读日记", "expect_tool": "search_vault 或 read_folder 或 read_file"},
        ])
        trace = {"tool_calls": _tc("read_folder"), "final_output": "done"}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["steps"][0]["ok"] is True

    def test_unordered_step_allows_earlier_call(self):
        case = self._case([
            {"step": "生成计划", "expect": "输出格式为计划列表（含标题/优先级）"},
            {"step": "读日记", "expect_tool": "read_folder", "ordered": False},
        ])
        # read_folder 在唯一锚点之前也不罚（ordered=false 不推进锚点）
        trace = {"tool_calls": _tc("read_folder"), "final_output": "计划：\n1. [high] 事项"}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["steps"][1]["ok"] is True

    def test_state_assert_snapshot_delta(self, monkeypatch):
        monkeypatch.setattr(trace_mod, "_snapshot_todos",
                            lambda: [{"title": "新任务", "status": "pending"}])
        case = self._case([
            {"step": "新增", "expect_tool": "update_task_status",
             "state_assert": "todo 出现且 status=pending"},
        ])
        trace = {"tool_calls": _tc("update_task_status"), "final_output": "done"}
        before = {"todos": [], "episodic_count": 0}
        wf = _check_expected_workflow(case, trace, before)
        assert wf["steps"][0]["ok"] is True
        # 显式"新增 N 条"严格比对增量：todo 运行前已存在 → 不算新增
        monkeypatch.setattr(trace_mod, "_snapshot_todos",
                            lambda: [{"title": "旧任务", "status": "pending"}])
        wf2 = _check_expected_workflow(
            self._case([{"step": "新增", "state_assert": "todo 新增 1 条"}]),
            trace, {"todos": [{"title": "旧任务", "status": "pending"}],
                    "episodic_count": 0})
        assert wf2["steps"][0]["ok"] is False

    def test_state_assert_status_mismatch_fails(self, monkeypatch):
        monkeypatch.setattr(trace_mod, "_snapshot_todos",
                            lambda: [{"title": "新任务", "status": "pending"}])
        case = self._case([
            {"step": "新增", "state_assert": "todo 出现且 status=done"},
        ])
        trace = {"tool_calls": _tc("update_task_status"), "final_output": "done"}
        wf = _check_expected_workflow(case, trace, {"todos": []})
        assert wf["steps"][0]["ok"] is False

    def test_unknown_step_assertion_does_not_pass(self, monkeypatch):
        # 判不准的终态断言 → step unknown → complete=False（不放行）
        monkeypatch.setattr(trace_mod, "_snapshot_todos", lambda: [])
        case = self._case([
            {"step": "汇报", "expect": "完全无法判定的断言xyz"},
        ])
        trace = {"tool_calls": [], "final_output": "done"}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["steps"][0]["ok"] is None
        assert wf["complete"] is False
        assert wf["unknown_steps"] == 1

    def test_step_without_assertions_is_unknown(self):
        case = self._case([{"step": "空步骤"}])
        wf = _check_expected_workflow(case, {"tool_calls": [], "final_output": ""}, {})
        assert wf["steps"][0]["ok"] is None

    def test_no_workflow_returns_none(self):
        assert _check_expected_workflow({"input": "x"}, {"tool_calls": []}, {}) is None


# ---------------------------------------------------------------------------
# 3. 快照增量检查 / 多意图拆分 / 报告聚合
# ---------------------------------------------------------------------------

class TestStateDelta:
    def test_todo_appear_and_status(self, monkeypatch):
        monkeypatch.setattr(trace_mod, "_snapshot_todos",
                            lambda: [{"title": "A", "status": "in_progress"}])
        ok, _ = _check_state_delta("todo 出现且 status=in_progress", {"todos": []})
        assert ok is True

    def test_todo_absent_fails(self, monkeypatch):
        monkeypatch.setattr(trace_mod, "_snapshot_todos", lambda: [])
        ok, reason = _check_state_delta("todo 出现", {"todos": []})
        assert ok is False

    def test_episodic_delta(self, monkeypatch):
        monkeypatch.setattr(trace_mod, "_snapshot_episodic_count", lambda: 5)
        ok, reason = _check_state_delta("episodic 新增 2 条", {"episodic_count": 3})
        assert ok is True
        ok, _ = _check_state_delta("episodic 新增 3 条", {"episodic_count": 3})
        assert ok is False

    def test_non_delta_syntax_returns_none(self):
        ok, _ = _check_state_delta("输出格式为计划列表（含标题/优先级）", {})
        assert ok is None


class TestIntentCompletion:
    def _case(self, intents, workflow):
        return {"intents": intents, "expected_workflow": workflow}

    def test_partial_intent_completion(self):
        case = self._case(
            ["新增任务", "合并重复待办", "状态更新"],
            [{"step": "s1", "intent": "新增任务"},
             {"step": "s2", "intent": "合并重复待办"},
             {"step": "s3", "intent": "状态更新"}])
        wf = {"steps": [{"ok": True}, {"ok": True}, {"ok": False}]}
        ic = _compute_intent_completion(case, wf)
        assert ic["completed_intents"] == 2
        assert ic["total_intents"] == 3
        assert ic["per_intent"]["状态更新"] is False

    def test_unmapped_intent_not_counted(self):
        case = self._case(
            ["意图A", "未被步骤引用的意图"],
            [{"step": "s1", "intent": "意图A"}])
        wf = {"steps": [{"ok": True}]}
        ic = _compute_intent_completion(case, wf)
        assert ic["total_intents"] == 1  # 未映射意图不计入分母

    def test_requires_two_intents(self):
        assert _compute_intent_completion({"intents": ["a"]},
                                          {"steps": [{"ok": True}]}) is None

    def test_steps_without_intent_mapping_returns_none(self):
        case = self._case(["a", "b"], [{"step": "s1"}, {"step": "s2"}])
        assert _compute_intent_completion(case, {"steps": [{"ok": True}]}) is None


class TestAggregateWorkflow:
    def test_aggregation(self):
        results = [
            {"intent": "wf1", "suspicious_pass": False,
             "workflow": {"total_steps": 3, "completed_steps": 3, "complete": True}},
            {"intent": "wf2", "suspicious_pass": True,
             "workflow": {"total_steps": 2, "completed_steps": 1, "complete": False}},
            {"intent": "plain", "suspicious_pass": False, "workflow": None},
            {"intent": "mi", "suspicious_pass": False, "workflow": None,
             "intent_completion": {"completed_intents": 1, "total_intents": 2}},
        ]
        agg = _aggregate_workflow(results)
        assert agg["workflow_cases"] == 2
        assert agg["workflow_completion_rate"] == 80.0  # 4/5 步
        assert agg["workflow_case_pass_rate"] == 50.0
        assert agg["multi_intent_completion_rate"] == 50.0
        assert agg["suspicious_pass_cases"] == ["wf2"]

    def test_empty_results(self):
        agg = _aggregate_workflow([])
        assert agg["workflow_cases"] == 0
        assert agg["workflow_completion_rate"] is None
        assert agg["multi_intent_completion_rate"] is None


# ---------------------------------------------------------------------------
# 4. scorecard L1B 指标
# ---------------------------------------------------------------------------

@pytest.fixture
def benchmark_dir(tmp_path, monkeypatch):
    """把 scorecard 的 benchmark 目录指到 tmp，写入合成报告。"""
    import app.agent.scorecard as sc
    d = tmp_path / "benchmark"
    d.mkdir()
    monkeypatch.setattr(sc, "_BENCHMARK_DIR", d)
    return d


def _write_benchmark(directory, results):
    report = {"pass_rate": 100.0, "total_cases": len(results), "results": results}
    f = sorted(directory.glob("benchmark_*.json"))
    (directory / "benchmark_2099_01_01.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")


class TestScorecardL1B:
    def test_workflow_completion_score(self, benchmark_dir):
        from app.agent.scorecard import _score_multi_intent_completion, _score_workflow_completion
        _write_benchmark(benchmark_dir, [
            {"intent": "wf", "workflow": {"total_steps": 4, "completed_steps": 3,
                                          "complete": False}},
        ])
        r = _score_workflow_completion()
        # 步骤 3/4=75%, case 全链路 0% → 50/50 加权 = 37.5
        assert r["score"] == 37.5
        assert "3/4" in r["detail"]

    def test_multi_intent_score_with_partial(self, benchmark_dir):
        from app.agent.scorecard import _score_multi_intent_completion
        _write_benchmark(benchmark_dir, [
            {"intent": "mi", "intent_completion": {"completed_intents": 1,
                                                   "total_intents": 3}},
        ])
        r = _score_multi_intent_completion()
        assert r["score"] == 33.3
        assert "半失败" in r["detail"]

    def test_no_workflow_cases_skipped(self, benchmark_dir):
        from app.agent.scorecard import _score_multi_intent_completion, _score_workflow_completion
        _write_benchmark(benchmark_dir, [{"intent": "plain", "workflow": None}])
        assert _score_workflow_completion()["score"] is None
        assert _score_multi_intent_completion()["score"] is None


# ---------------------------------------------------------------------------
# 5. challenge 用例 schema 完整性（expected_workflow 可被 grader 消费）
# ---------------------------------------------------------------------------

class TestChallengeCaseSchema:
    def test_challenge_cases_have_workflow(self):
        from app.agent.trace import load_test_cases
        cases = [c for c in load_test_cases(tier="challenge")
                 if c.get("id", "").startswith("routing-")]
        assert cases, "challenge/complex_task_ops.json 加载失败"
        for c in cases:
            wf = c.get("expected_workflow")
            assert wf and len(wf) >= 2, f"{c['id']} 缺 expected_workflow"
            assert len(c.get("intents", [])) >= 2, f"{c['id']} 缺 intents"
            # 每个意图都有 step 承载（保证 Multi-intent 可拆分）
            step_intents = {s.get("intent") for s in wf}
            assert set(c["intents"]) <= step_intents

    def test_workflow_grader_consumes_challenge_case(self):
        """合成一条'全部完成'的 trace，grader 应判 complete（校验 JSON 可被真实消费）。"""
        from app.agent.trace import load_test_cases
        case = next(c for c in load_test_cases(tier="challenge")
                    if c.get("id") == "routing-comprehensive-plan-with-todo")
        trace = {
            "route": "plan",
            "tool_calls": _tc("read_topic_memory", "search_vault"),
            "final_output": "明日计划：\n1. [high] 秋招准备（来自待办）\n2. [medium] 面试复盘",
            "memory_updates": [],
        }
        result_data = {"items": [
            {"title": "秋招准备", "priority": "high", "source": "task_op"},
            {"title": "面试复盘", "priority": "medium", "source": "goal"},
        ]}
        wf = _check_expected_workflow(case, trace, {})
        assert wf["total_steps"] == 4
        assert wf["completed_steps"] >= 3  # 终态断言依赖真实 agent_data，宽松下界防误报
