"""Checkpoint 改造回归测试 (2026-08)。

验证目标:
  1. run_id (thread_id) 跨调用稳定, 不再被 trace_id 覆盖
  2. chatbot 历史经 SqliteSaver 自动累积 —— 不再手工 load_messages 注入
  3. system prompt 每轮唯一、历史消息不重复
  4. 不同 thread 状态隔离
  5. 四个路由 (chatbot/plan/reflect/memory) 均正常工作
  6. append_exchange 的每 3 轮摘要机制仍工作
  7. clear_session 同步清除 checkpoint 线程状态
"""
from __future__ import annotations

from langchain_core.messages import SystemMessage

from app.agent.checkpoint import get_checkpointer
from app.agent.graphs.orchestrator import run_orchestrator
from app.agent.session import append_exchange, clear_session


def _chat_calls(model) -> list:
    """取出 chatbot 图 llm 节点的调用 (输入为消息列表的那些)。"""
    return [c for c in model.calls if isinstance(c, list)]


def _non_system(messages) -> list:
    return [m for m in messages if not isinstance(m, SystemMessage)]


def _texts(messages) -> list[str]:
    return [str(getattr(m, "content", "")) for m in messages]


def test_run_id_stable_across_calls(mock_llm, isolated_data):
    r1 = run_orchestrator("第一轮测试", thread_id="reg_stable")
    r2 = run_orchestrator("第二轮测试", thread_id="reg_stable")
    assert r1["success"] and r2["success"]
    assert r1["run_id"] == "reg_stable"
    assert r2["run_id"] == "reg_stable"


def test_history_accumulates_in_checkpoint(mock_llm, isolated_data):
    """同一 thread 两次调用: 第二次 LLM 自动看到第一轮的 human+ai 消息。"""
    run_orchestrator("第一轮: 我叫塔塔", thread_id="reg_hist")
    chat_calls = _chat_calls(mock_llm)
    assert len(chat_calls) == 1
    assert len(_non_system(chat_calls[0])) == 1  # 首轮只有当前消息

    run_orchestrator("第二轮: 我叫什么", thread_id="reg_hist")
    chat_calls = _chat_calls(mock_llm)
    assert len(chat_calls) == 2
    msgs = chat_calls[1]
    texts = _texts(msgs)
    # 历史自动累积: 第一轮 human + 第一轮 ai 回答 + 第二轮 human
    assert "第一轮: 我叫塔塔" in texts
    assert "测试回答" in texts
    assert "第二轮: 我叫什么" in texts
    assert len(_non_system(msgs)) == 3
    # system 唯一且在开头
    systems = [m for m in msgs if isinstance(m, SystemMessage)]
    assert len(systems) == 1
    assert isinstance(msgs[0], SystemMessage)


def test_thread_isolation(mock_llm, isolated_data):
    run_orchestrator("甲的消息", thread_id="reg_a")
    mock_llm.calls.clear()
    run_orchestrator("乙的消息", thread_id="reg_b")
    run_orchestrator("乙的第二条", thread_id="reg_b")
    chat_calls = _chat_calls(mock_llm)
    last = chat_calls[-1]
    texts = _texts(last)
    assert "乙的消息" in texts and "乙的第二条" in texts
    assert "甲的消息" not in texts


def test_system_not_duplicated_over_turns(mock_llm, isolated_data):
    for i in range(3):
        run_orchestrator(f"第{i+1}轮", thread_id="reg_sys")
    chat_calls = _chat_calls(mock_llm)
    last = chat_calls[-1]
    systems = [m for m in last if isinstance(m, SystemMessage)]
    assert len(systems) == 1
    # 3 轮 human + 2 轮 ai 回答
    assert len(_non_system(last)) == 5


def test_all_routes_still_work(mock_llm, isolated_data):
    for route in ("chatbot", "plan", "reflect", "memory"):
        mock_llm.route = route
        r = run_orchestrator(f"测试{route}路由", thread_id=f"reg_{route}")
        assert r["success"], f"{route} 路由失败: {r.get('error')}"
        assert r["route"] == route


def test_append_exchange_summary_still_works(mock_llm, isolated_data):
    from app.agent.memory_store import _get_conn

    sid = "session_test_isolated"
    append_exchange(sid, "用户问题一：今天有什么安排吗", "回答一")
    append_exchange(sid, "用户问题二：明天天气怎么样", "回答二")
    append_exchange(sid, "用户问题三：帮我记录一个偏好", "回答三")  # 第 3 轮触发 LLM 摘要
    conn = _get_conn()
    n = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE memory_type='conversation' AND source='session_summary'"
    ).fetchone()["c"]
    assert n >= 1
    # 洞察分层：stable_insights 写入 episodic（tags 含 insight）
    n_insights = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE memory_type='episodic' AND tags LIKE '%insight%'"
    ).fetchone()["c"]
    assert n_insights >= 1
    n_msgs = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE session_id=?", (sid,)
    ).fetchone()["c"]
    assert n_msgs == 6


def test_clear_session_clears_checkpoint(mock_llm, isolated_data):
    run_orchestrator("要清空的内容", thread_id="reg_clear")
    cp = get_checkpointer()
    assert cp.get_tuple({"configurable": {"thread_id": "chat_reg_clear"}}) is not None
    clear_session("reg_clear")
    assert cp.get_tuple({"configurable": {"thread_id": "chat_reg_clear"}}) is None


def test_checkpoint_history_gets_compressed(monkeypatch, isolated_data):
    """checkpoint 累积历史超 HISTORY_BUDGET 时, gather_context 用 replace 压缩早期轮次。

    覆盖: CLI/Web 路径 (历史由 checkpoint 累积) 失去 orchestrator 压缩后的
    新守卫 —— 压缩必须发生在 chatbot 图内部, 否则长会话上下文无限膨胀。
    """
    import app.agent.graphs.llm as llm_mod
    from langchain_core.messages import AIMessage

    from conftest import FakeModel

    long_answer = "这是很长的回答内容。" * 40  # ~360 字/轮, 15 轮后远超 4000 token

    class LongModel(FakeModel):
        def invoke(self, input_):
            self.calls.append(input_)
            text = self._text(input_)
            if "route it to the correct sub-agent" in text:
                return AIMessage(content='{"route": "chatbot", "reason": "test"}')
            return AIMessage(content=long_answer)

    fake = LongModel()
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda *a, **k: fake)
    monkeypatch.setattr("app.agent.graphs.orchestrator.get_chat_model", lambda *a, **k: fake)
    monkeypatch.setattr("app.agent.graphs.chatbot_graph.get_chat_model", lambda *a, **k: fake)

    for i in range(16):
        r = run_orchestrator(f"第{i+1}轮", thread_id="reg_compress")
        assert r["success"], f"第{i+1}轮失败: {r.get('error')}"

    chat_calls = [c for c in fake.calls if isinstance(c, list)]
    last = chat_calls[-1]
    # 未压缩: 16 human + 15 ai = 31 条; 压缩后显著减少
    assert len(last) < 20, f"历史未被压缩: {len(last)} 条消息"
    texts = _texts(last)
    assert any("早期会话" in t for t in texts), "缺少压缩摘要标记"
