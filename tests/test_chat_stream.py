# -*- coding: utf-8 -*-
"""流式编排器 + SSE 事件协议回归测试。

覆盖:
  1. stream_orchestrator 事件序列:planner_done → step* → final(结构与 run_orchestrator 一致)
  2. chatbot 主回答在流式上下文中逐 token 推出,且 final.result 完整
  3. 非 stream 能力的模型(mock)在流式调用下安全降级到 invoke(无 token 帧但不崩溃)
"""
from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, AIMessageChunk

from app.agent.graphs.orchestrator import run_orchestrator, stream_orchestrator


def _collect_events(text, thread_id):
    async def run():
        return [ev async for ev in stream_orchestrator(text, thread_id=thread_id)]
    return asyncio.run(run())


def test_stream_event_sequence_and_final_structure(mock_llm, isolated_data):
    """事件序列:planner_done 先行、final 结构与 run_orchestrator 返回一致。"""
    events = _collect_events("随便聊聊", "st_seq")
    types = [e["type"] for e in events]
    assert types[0] == "planner_done"
    assert types[-1] == "final"
    assert "token" not in types  # FakeModel 无 stream 能力 → 安全降级,无 token 帧

    planner = events[0]
    assert planner["route"] == "chatbot"
    final = events[-1]
    # final 与 run_orchestrator 返回结构逐字段一致
    sync = run_orchestrator("随便聊聊", thread_id="st_seq_sync")
    for key in ("success", "route", "route_reason", "result", "result_data",
                "tasks", "task_results", "latency_ms", "run_id", "trace_id"):
        assert key in final, f"final 缺少字段 {key}"
    assert final["success"] is True
    assert final["route"] == sync["route"]
    assert final["trace_id"]  # execute 节点内 trace 照常记录


def test_stream_tokens_reach_consumer_and_final_result_complete(
        mock_llm, isolated_data, monkeypatch):
    """带 stream 能力的模型:token 逐帧推出且拼接 == final.result。"""
    import app.agent.graphs.chatbot_graph as cbg

    class FakeStreamModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, input_):
            self.calls += 1
            return AIMessage(content="流式回答完成")

        def stream(self, input_):
            for piece in ("流式", "回答", "完成"):
                yield AIMessageChunk(content=piece)

    fsm = FakeStreamModel()
    monkeypatch.setattr(cbg, "get_chat_model", lambda *a, **k: fsm)

    events = _collect_events("讲个故事", "st_token")
    tokens = [e["text"] for e in events if e["type"] == "token"]
    final = events[-1]

    assert "".join(tokens) == "流式回答完成"
    assert final["type"] == "final"
    assert final["result"] == "流式回答完成"
    assert fsm.calls == 0  # 流式成功时不降级 invoke


def test_stream_fallback_when_model_stream_fails(mock_llm, isolated_data, monkeypatch):
    """stream 中途抛错 → 降级 invoke,final 仍完整(不吞掉回答)。"""
    import app.agent.graphs.chatbot_graph as cbg

    class HalfStreamModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, input_):
            return AIMessage(content="降级回答OK")

        def stream(self, input_):
            yield AIMessageChunk(content="半个")
            raise RuntimeError("stream boom")

    monkeypatch.setattr(cbg, "get_chat_model", lambda *a, **k: HalfStreamModel())

    events = _collect_events("再来一个", "st_fallback")
    final = events[-1]
    assert final["success"] is True
    assert final["result"] == "降级回答OK"


def test_sse_endpoint_framing(mock_llm, isolated_data):
    """/chat/stream SSE 帧协议:content-type、data: 帧、[DONE] 结束、事件序列。"""
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes_agent_v2 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)  # 不进 lifespan，避免启动期副作用
    with client.stream(
        "POST", "/agent/v2/chat/stream",
        json={"text": "随便聊聊", "event_id": "sse_test"},
    ) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())

    frames = [ln[6:] for ln in body.split("\n") if ln.startswith("data: ")]
    assert frames, "SSE 响应无 data 帧"
    assert frames[-1] == "[DONE]"
    events = [_json.loads(f) for f in frames[:-1]]
    assert events[0]["type"] == "planner_done"
    assert events[-1]["type"] == "final"
    assert events[-1]["success"] is True
    assert events[-1]["trace_id"]
