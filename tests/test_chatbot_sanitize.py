# -*- coding: utf-8 -*-
"""chatbot 消息序列 sanitize 测试（badcase: candidate-captured-20260828173328）。

根因链（2026-08-28 定位）:
  会话历史超预算 → gather_context_node 压力压缩按条数切片
  （keep = messages[-KEEP_RECENT_TURNS*2:]）→ 早期 ai(tool_calls) 被摘要
  替代，但其后的 tool 结果留在 keep 开头 → 孤立 tool 消息 →
  LLM 400 "Messages with role 'tool' must be a response to a preceding
  message with 'tool_calls'" → checkpoint 持久化坏序列，之后每次进会话都挂。

修复: _sanitize_messages 在压力压缩后与 LLM 调用前丢弃孤立 tool 消息。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graphs.chatbot_graph import _sanitize_messages, build_chatbot_graph


def _ai_calls(tool_names, content="思考"):
    return AIMessage(content=content, tool_calls=[
        {"name": n, "args": {}, "id": f"call_{n}", "type": "tool_call"}
        for n in tool_names])


def test_sanitize_drops_orphan_tool_at_head():
    """压缩场景：摘要 AI 消息后面的孤立 tool 被丢弃。"""
    msgs = [
        AIMessage(content="[早期会话摘要]"),
        ToolMessage(content="搜索八股结果", tool_call_id="call_x"),
        HumanMessage(content="新问题"),
    ]
    out = _sanitize_messages(msgs)
    assert len(out) == 2
    assert all(getattr(m, "type") != "tool" for m in out)


def test_sanitize_keeps_valid_pairing():
    """正常 ai(tool_calls) → tool 配对保留。"""
    msgs = [
        _ai_calls(["search_vault"]),
        ToolMessage(content="结果", tool_call_id="call_search_vault"),
        HumanMessage(content="继续"),
    ]
    out = _sanitize_messages(msgs)
    assert len(out) == 3


def test_sanitize_counts_multiple_tool_calls():
    """一条 ai 多个 tool_calls → 对应多条 tool 消息全部保留。"""
    msgs = [
        _ai_calls(["search_vault", "read_file"]),
        ToolMessage(content="r1", tool_call_id="call_search_vault"),
        ToolMessage(content="r2", tool_call_id="call_read_file"),
    ]
    out = _sanitize_messages(msgs)
    assert len(out) == 3


def test_sanitize_drops_middle_and_trailing_orphans():
    """中间与末尾的孤立 tool 都被丢弃。"""
    msgs = [
        HumanMessage(content="h"),
        _ai_calls(["a"]),
        ToolMessage(content="t1", tool_call_id="call_a"),
        ToolMessage(content="中间孤儿", tool_call_id="call_b"),
        HumanMessage(content="h2"),
        ToolMessage(content="尾巴孤儿", tool_call_id="call_c"),
    ]
    out = _sanitize_messages(msgs)
    roles = [getattr(m, "type") for m in out]
    assert roles == ["human", "ai", "tool", "human"]


def test_chatbot_graph_invokes_without_orphan_tool(mock_llm, isolated_data):
    """端到端：含孤立 tool 的坏历史 → graph 调用不抛错，LLM 收到的序列已修复。"""
    history = [
        AIMessage(content="[早期会话摘要]"),
        ToolMessage(content="搜索八股结果（孤立）", tool_call_id="call_orphan"),
        HumanMessage(content="在幂等的八股下面回答：那这个幂等能否完全做到防止死循环？"),
    ]
    graph = build_chatbot_graph()
    result = graph.invoke(
        {"messages": history, "system": "test system"},
        {"configurable": {"thread_id": "sanitize_test"}},
    )
    last = result["messages"][-1]
    assert hasattr(last, "content") and last.content
    # FakeModel 收到的 llm_messages 里不应有孤立 tool
    sent = mock_llm.calls[-1]
    roles = [getattr(m, "type", "?") for m in sent] if isinstance(sent, list) else []
    assert "tool" not in roles
