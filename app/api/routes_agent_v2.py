"""Routes for the LangGraph Agent system — Orchestrator + 5 sub-agents.

  POST /agent/v2/chat          对话式（路由到 orchestrator）
  POST /agent/v2/chat/stream   对话式流式（SSE：step/token 增量 + final 完整结果）
  POST /agent/v2/plan         每日计划（LLM 驱动版）
  POST /agent/v2/reflect      反思分析
  POST /agent/v2/memory       记忆管理
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.graphs.orchestrator import run_orchestrator, stream_orchestrator
from app.agent.graphs.plan_graph import run_plan_graph
from app.agent.graphs.reflect_graph import run_reflect
from app.agent.graphs.memory_graph import run_memory_agent

router = APIRouter(prefix="/agent/v2", tags=["Agent v2"])


class ChatRequest(BaseModel):
    text: str
    user_id: str = "default_user"
    conversation: list[str] | None = None
    # 事件ID：会话窗口级唯一ID（前端 sess_*）。同窗口多轮共享同一执行线程，
    # chatbot 子图 checkpoint 按它累积历史；缺省则每次请求独立线程（兼容旧客户端）。
    event_id: str | None = None


class PlanRequest(BaseModel):
    user_id: str = "default_user"
    date: str | None = None


class ReflectRequest(BaseModel):
    subject: str = "general"
    content: str
    user_id: str = "default_user"


class MemoryRequest(BaseModel):
    text: str
    user_id: str = "default_user"
    memory_type: str | None = None


@router.post("/chat")
def agent_chat(req: ChatRequest) -> dict[str, Any]:
    """Orchestrator — 自动路由到正确的子 Agent（一次性返回）。"""
    return run_orchestrator(
        input_text=req.text,
        user_id=req.user_id,
        conversation=req.conversation,
        # 事件ID → 执行线程：同窗口续聊共享 checkpoint 历史
        thread_id=(req.event_id or "").strip()[:128] or None,
    )


@router.post("/chat/stream")
async def agent_chat_stream(req: ChatRequest) -> StreamingResponse:
    """Orchestrator — 流式对话（SSE）。

    事件帧 `data: {json}\\n\\n`，序列：planner_done → step* → token* → final；
    final 与 /chat 返回结构一致（前端可无缝复用 adapter）。以 `data: [DONE]` 结束。
    """
    thread_id = (req.event_id or "").strip()[:128] or None

    async def event_gen():
        try:
            async for ev in stream_orchestrator(
                input_text=req.text,
                user_id=req.user_id,
                conversation=req.conversation,
                thread_id=thread_id,
            ):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:  # 流中异常也以 final 帧告知，前端可降级/提示
            yield "data: " + json.dumps(
                {"type": "final", "success": False, "route": "error",
                 "result": str(exc), "error": str(exc)},
                ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 反代时不缓冲，保证逐帧到达
        },
    )


@router.post("/plan")
def agent_plan(req: PlanRequest) -> dict[str, Any]:
    """每日计划（LLM 驱动版）。"""
    return run_plan_graph(user_id=req.user_id, plan_date=req.date)


@router.post("/reflect")
def agent_reflect(req: ReflectRequest) -> dict[str, Any]:
    """反思分析。"""
    return run_reflect(subject=req.subject, content=req.content, user_id=req.user_id)


@router.post("/memory")
def agent_memory(req: MemoryRequest) -> dict[str, Any]:
    """记忆管理。"""
    return run_memory_agent(
        trigger_text=req.text,
        user_id=req.user_id,
        memory_type=req.memory_type,
    )


@router.get("/tools")
def list_tools() -> dict[str, Any]:
    """查看所有已注册的工具及其 schema。"""
    from app.agent.graphs.tools import get_registry
    tools = get_registry().list_tools_for_llm()
    return {"tool_count": len(tools), "tools": tools}


@router.get("/tools/stats")
def tool_stats() -> dict[str, Any]:
    """工具调用统计。"""
    from app.agent.graphs.tools import get_registry
    return get_registry().get_tool_stats()


@router.get("/tools/audit")
def tool_audit(limit: int = 50) -> dict[str, Any]:
    """工具调用审计日志。"""
    from app.agent.graphs.tools import get_registry
    return {"audit": get_registry().get_audit_log(limit=limit)}


@router.get("/tools/status")
def tool_status() -> dict[str, Any]:
    """工具注册状态。"""
    from app.agent.graphs.tools import get_registry
    reg = get_registry()
    return {
        "native_tools": len(reg.list_tools_for_llm()),
        "tools": [t["name"] for t in reg.list_tools_for_llm()],
        "status": "native_ready",
    }


@router.get("/self-eval")
def agent_self_eval() -> dict[str, Any]:
    from app.agent.self_eval import run_self_eval
    return run_self_eval()

@router.get("/traces")
def list_traces(limit: int = 50) -> dict[str, Any]:
    """查看最近 trace 记录。"""
    from app.agent.trace import load_all_traces, get_trace_stats
    traces = load_all_traces(limit=limit)
    return {"traces": traces, "stats": get_trace_stats(traces)}


@router.post("/benchmark")
def run_benchmark() -> dict[str, Any]:
    """运行回归测试套件。"""
    from app.agent.trace import run_benchmark_suite
    return run_benchmark_suite()
