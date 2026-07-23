"""Routes for the LangGraph Agent system — Orchestrator + 5 sub-agents.

  POST /agent/v2/chat         对话式（路由到 orchestrator）
  POST /agent/v2/plan         每日计划（LLM 驱动版）
  POST /agent/v2/reflect      反思分析
  POST /agent/v2/memory       记忆管理
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.agent.graphs.orchestrator import run_orchestrator
from app.agent.graphs.plan_graph import run_plan_graph
from app.agent.graphs.reflect_graph import run_reflect
from app.agent.graphs.memory_graph import run_memory_agent

router = APIRouter(prefix="/agent/v2", tags=["Agent v2"])


class ChatRequest(BaseModel):
    text: str
    user_id: str = "default_user"
    conversation: list[str] | None = None


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
    """Orchestrator — 自动路由到正确的子 Agent。"""
    return run_orchestrator(
        input_text=req.text,
        user_id=req.user_id,
        conversation=req.conversation,
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


@router.get("/mcp/status")
def mcp_status() -> dict[str, Any]:
    """MCP 服务器连接状态。"""
    from app.agent.graphs.tools import get_registry
    reg = get_registry()
    # TODO: 实际 MCP 连接状态
    return {
        "native_tools": len([t for t in reg.list_tools_for_llm() if t.get("source", "native") == "native"]),
        "mcp_tools": len([t for t in reg.list_tools_for_llm() if t.get("source") == "mcp"]),
        "status": "native_ready",
        "mcp_servers_registered": len(reg._mcp_servers) if hasattr(reg, '_mcp_servers') else 0,
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
