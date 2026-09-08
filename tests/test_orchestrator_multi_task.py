# -*- coding: utf-8 -*-
"""Planner + Task Graph 多任务编排回归测试（2026-08 架构改造）。

覆盖:
  1. 单意图 → 恰好 1 个 task，route 语义与旧单路由完全等价
  2. 多意图 → 顺序执行多个子 agent，State Merge 汇总输出
  3. planner 输出解析失败 → fallback 单任务 chatbot
  4. 部分子任务失败 → 其余任务结果保留，整体 success=False
  5. 非法 agent 名 → 规范化为 chatbot
"""
from __future__ import annotations

import pytest

from app.agent.graphs.orchestrator import run_orchestrator


def test_single_intent_single_task_route_equivalent(mock_llm, isolated_data):
    """单意图：FakeModel 返回旧格式 {"route": ...} → 1 个任务，route 与旧行为一致。"""
    for route in ("chatbot", "plan", "reflect", "memory"):
        mock_llm.route = route
        r = run_orchestrator(f"测试{route}路由", thread_id=f"mt_{route}")
        assert r["success"], f"{route} 失败: {r.get('error')}"
        assert r["route"] == route
        assert len(r["tasks"]) == 1
        assert r["tasks"][0]["agent"] == route


def test_multi_intent_executes_all_tasks_in_order(mock_llm, isolated_data):
    """多意图：memory + chatbot 两个子任务顺序执行，State Merge 输出合并。"""
    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer"},
        {"agent": "chatbot", "instruction": "帮我推荐RAG学习资料"},
    ]
    r = run_orchestrator("记住我的目标，然后推荐RAG资料", thread_id="mt_multi")
    assert r["success"], r.get("error")
    assert r["route"] == "memory"            # 主路由 = 第一个任务
    assert len(r["tasks"]) == 2
    assert len(r["task_results"]) == 2
    # State Merge：两个子任务输出都在最终回复里
    assert "【子任务 1: memory】" in r["result"]
    assert "【子任务 2: chatbot】" in r["result"]
    # memory 子任务真实写入（FakeModel 判 skip → 显式指令强制写入）
    from app.agent.agent_data_service import read_memory
    episodic = read_memory("episodic")
    contents = " ".join(str(e.get("content", "")) for e in episodic.get("entries", []))
    assert "offer" in contents


def test_planner_parse_failure_falls_back_to_chatbot(mock_llm, isolated_data, monkeypatch):
    """planner 输出无法解析 → fallback 单任务 chatbot（不崩溃）。"""
    from app.agent.graphs import orchestrator as orch
    from langchain_core.messages import AIMessage

    class BadModel:
        def invoke(self, prompt):
            return AIMessage(content="this is not json")

    monkeypatch.setattr(orch, "get_chat_model", lambda *a, **k: BadModel())
    r = run_orchestrator("随便聊聊", thread_id="mt_fallback")
    assert r["success"]
    assert r["route"] == "chatbot"
    assert len(r["tasks"]) == 1
    assert r["tasks"][0]["agent"] == "chatbot"


def test_partial_task_failure_keeps_other_results(mock_llm, isolated_data, monkeypatch):
    """部分子任务失败：不中断后续任务，success=False，但成功任务的结果保留。"""
    from app.agent.graphs import orchestrator as orch

    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer"},
        {"agent": "reflect", "instruction": "反思我的进展"},
    ]

    def boom(state, trace, step, text, user_id):
        raise RuntimeError("reflect boom")

    monkeypatch.setattr(orch, "_run_reflect_task", boom)
    r = run_orchestrator("记住目标并反思", thread_id="mt_partial")
    assert r["success"] is False
    assert len(r["task_results"]) == 2
    assert r["task_results"][0]["success"] is True
    assert r["task_results"][1]["success"] is False
    assert "reflect boom" in r["result"]
    # 成功任务的结果仍在合并输出中
    assert "【子任务 1: memory】" in r["result"]


def test_invalid_agent_in_task_normalized_to_chatbot(mock_llm, isolated_data):
    """非法 agent 名 → 规范化为 chatbot。"""
    mock_llm.tasks = [
        {"agent": "not-an-agent", "instruction": "随便聊聊"},
    ]
    r = run_orchestrator("测试", thread_id="mt_invalid")
    assert r["success"]
    assert len(r["tasks"]) == 1
    assert r["tasks"][0]["agent"] == "chatbot"


def test_parallel_same_stage_non_chatbot_runs_concurrently(
        mock_llm, isolated_data, monkeypatch):
    """非 chatbot 同 stage 任务真正并行（起始时间重叠），结果保序。"""
    import threading
    import time

    from app.agent.graphs import orchestrator as orch

    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer", "stage": 1},
        {"agent": "plan", "instruction": "帮我生成今天的计划", "stage": 1},
    ]
    stamps: list[float] = []
    lock = threading.Lock()
    real = orch._execute_single_task

    def slow(state, trace, step, agent, instruction, user_id):
        with lock:
            stamps.append(time.monotonic())
        time.sleep(0.3)
        return real(state, trace, step, agent, instruction, user_id)

    monkeypatch.setattr(orch, "_execute_single_task", slow)
    r = run_orchestrator("记住我的目标并生成计划", thread_id="mt_par")
    assert r["success"], r.get("error")
    assert len(r["task_results"]) == 2
    # 两个任务起始时间重叠 → 真正并行执行
    assert len(stamps) == 2
    assert abs(stamps[0] - stamps[1]) < 0.25, f"未并行: {stamps}"
    # 保序：task_results 与 tasks 顺序一致
    assert r["task_results"][0]["agent"] == "memory"
    assert r["task_results"][1]["agent"] == "plan"
    assert "【子任务 1: memory】" in r["result"]
    assert "【子任务 2: plan】" in r["result"]


def test_chatbot_in_group_forces_serial(mock_llm, isolated_data):
    """含 chatbot 的组强制串行（checkpoint 安全），行为正确。"""
    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer", "stage": 1},
        {"agent": "chatbot", "instruction": "帮我推荐RAG资料", "stage": 1},
    ]
    r = run_orchestrator("记住我的目标，然后推荐RAG资料", thread_id="mt_ser")
    assert r["success"], r.get("error")
    assert len(r["task_results"]) == 2
    assert r["task_results"][0]["agent"] == "memory"
    assert r["task_results"][1]["agent"] == "chatbot"
    assert "【子任务 1: memory】" in r["result"]
    assert "【子任务 2: chatbot】" in r["result"]


def test_stage_dependency_sequential(mock_llm, isolated_data):
    """跨 stage 依赖：memory(stage1) → plan(stage2) 顺序执行。"""
    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer", "stage": 1},
        {"agent": "plan", "instruction": "帮我生成今天的计划", "stage": 2},
    ]
    r = run_orchestrator("记住我的目标并生成计划", thread_id="mt_stage")
    assert r["success"], r.get("error")
    assert len(r["task_results"]) == 2
    assert r["task_results"][0]["agent"] == "memory"
    assert r["task_results"][1]["agent"] == "plan"


def test_missing_stage_defaults_to_sequential(mock_llm, isolated_data):
    """stage 缺失 → 归一为顺序递增（保守串行），行为正确。"""
    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer"},
        {"agent": "reflect", "instruction": "反思我的进展"},
    ]
    r = run_orchestrator("记住我的目标并反思", thread_id="mt_nostage")
    assert r["success"], r.get("error")
    assert [t["stage"] for t in r["tasks"]] == [1, 2]
    assert len(r["task_results"]) == 2
    assert r["task_results"][0]["agent"] == "memory"
    assert r["task_results"][1]["agent"] == "reflect"


def test_planner_retries_once_then_fallback_with_clarification(
        mock_llm, isolated_data, monkeypatch):
    """planner 持续解析失败 → 重试一次（共 2 次调用）→ 兜底 chatbot + 请用户澄清。"""
    from app.agent.graphs import orchestrator as orch
    from langchain_core.messages import AIMessage

    class BadModel:
        calls = 0

        def invoke(self, prompt):
            BadModel.calls += 1
            return AIMessage(content="this is not json")

    monkeypatch.setattr(orch, "get_chat_model", lambda *a, **k: BadModel())
    r = run_orchestrator("随便聊聊", thread_id="mt_retry_fallback")
    assert r["success"]
    assert BadModel.calls == 2                       # 恰好重试一次，不无限重试
    assert r["route"] == "chatbot"
    assert "澄清" in r["tasks"][0]["instruction"]    # 兜底任务带澄清提示
    assert "retried" in r["route_reason"]


def test_planner_invalid_label_retries_then_recovers(
        mock_llm, isolated_data, monkeypatch):
    """非法 agent 标签视为解析失败：第 1 次坏输出 → 重试第 2 次恢复正常路由。"""
    from app.agent.graphs import orchestrator as orch
    from langchain_core.messages import AIMessage

    class FlakyModel:
        calls = 0

        def invoke(self, prompt):
            FlakyModel.calls += 1
            if FlakyModel.calls == 1:
                return AIMessage(content='{"route": "no_such_agent"}')
            return AIMessage(content='{"route": "plan", "reason": "recovered"}')

    monkeypatch.setattr(orch, "get_chat_model", lambda *a, **k: FlakyModel())
    r = run_orchestrator("帮我做今日计划", thread_id="mt_retry_recover")
    assert FlakyModel.calls == 2
    assert r["route"] == "plan"
    assert r["success"]


def test_planner_partial_invalid_label_keeps_valid_tasks(mock_llm, isolated_data):
    """部分任务标签非法仍放行（不触发重试）：非法项 coercion 为 chatbot。"""
    mock_llm.tasks = [
        {"agent": "memory", "instruction": "记住：我的目标是拿到AI offer"},
        {"agent": "junk-agent", "instruction": "其他"},
    ]
    r = run_orchestrator("记住并处理", thread_id="mt_partial_invalid")
    assert r["success"]
    assert r["tasks"][0]["agent"] == "memory"
    assert r["tasks"][1]["agent"] == "chatbot"
