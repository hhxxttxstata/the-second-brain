"""Agent 工具生态 — 统一的 Native Tool Registry。

架构:
─────────────────────────────────────────────────────
  Tools (agent 看到的扁平列表)
  ├── @tool search_vault           (Native)
  ├── @tool write_memory           (Native)
  └── ... (全部 Native)
─────────────────────────────────────────────────────
  Tool Registry
  └── native/   → 纯 Python 函数
─────────────────────────────────────────────────────
  Agent (LangGraph) 看到的是统一的 tool list
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from app.core.logging import logger


# ============================================================================
# 数据类型
# ============================================================================

@dataclass
class RegisteredTool:
    """统一的工具注册信息。"""
    name: str
    description: str
    schema_: dict[str, Any]      # JSON Schema
    source: str                  # "native"
    server_name: str | None      # 保留字段（MCP 已移除，恒为 None）
    risk_level: str              # "low" | "medium" | "high"
    side_effects: list[str]      # 副作用描述（审计用）
    handler: Any = None          # native callable


@dataclass
class ToolCallAudit:
    """每次工具调用的审计记录。"""
    tool_name: str
    params: dict[str, Any]
    result_summary: str
    latency_ms: int
    success: bool
    error: str | None
    risk_level: str
    timestamp: str


# ============================================================================
# 统一的 Tool Registry
# ============================================================================

class ToolRegistry:
    """所有工具的注册中心（Native）。"""

    def __init__(self) -> None:
        self._native_tools: dict[str, RegisteredTool] = {}
        self._audit_log: list[ToolCallAudit] = []
        self._started = False
        self._version = 0  # 每次注册 +1；未来图改为缓存单例时凭它判定是否需要重建

    # ── Native 工具注册 ──

    def register_native(self, tool: RegisteredTool) -> None:
        assert tool.source in ("native", "dynamic") and tool.handler is not None
        self._native_tools[tool.name] = tool
        self._version += 1
        logger.info("tool_registered", name=tool.name, source=tool.source,
                     risk=tool.risk_level)

    @property
    def version(self) -> int:
        """工具集版本号（动态工具注册后递增，供缓存/图重建联动）。"""
        return self._version

    # ── 获取工具列表 ──

    def list_tools_for_llm(self) -> list[dict[str, Any]]:
        """返回最终绑定给 LLM 的扁平工具列表。"""
        tools = []
        for t in self._native_tools.values():
            tools.append({
                "name": t.name,
                "description": t.description,
                "input_schema": t.schema_,
                "risk_level": t.risk_level,
            })
        return tools

    def get_native_tool(self, name: str) -> RegisteredTool | None:
        return self._native_tools.get(name)

    # ── 执行 ──

    async def execute(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """执行任意工具（Native 统一入口）。"""
        tool = self._native_tools.get(name)
        if not tool:
            return {"success": False, "error": f"未知工具: {name}"}

        start = time.monotonic()
        try:
            # vault 写操作 — 不再弹审批，直接执行
            # Agent 在 prompt 层面被要求谨慎删除、允许改写润色

            result = tool.handler(**params)

            latency = int((time.monotonic() - start) * 1000)
            result_text = str(result)[:200]
            self._audit(ToolCallAudit(
                tool_name=name, params=params, result_summary=result_text,
                latency_ms=latency, success=True, error=None,
                risk_level=tool.risk_level,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            ))

            # 自动记录到当前 trace
            try:
                from app.agent.trace import get_current_trace
                ct = get_current_trace()
                if ct is not None:
                    ct.add_tool_call(
                        name=name, params=params,
                        result_summary=result_text,
                        latency_ms=latency, success=True,
                    )
                    # memory 工具额外触发记忆更新记录
                    if name in ("write_memory", "update_task_status",
                                "write_topic_memory", "write_episodic_memory"):
                        preview = str(params.get("content")
                                      or params.get("task_title")
                                      or params.get("topic")
                                      or "")[:60]
                        ct.add_memory_update("tool_action", f"{name}: {preview}")
            except Exception:
                pass

            return {"success": True, "result": result}

        except Exception as exc:
            latency = int((time.monotonic() - start) * 1000)
            self._audit(ToolCallAudit(
                tool_name=name, params=params, result_summary="",
                latency_ms=latency, success=False, error=str(exc),
                risk_level=tool.risk_level,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            ))
            # 失败的工具调用也必须进 trace（否则'声称参数问题'/'工具必须可用'
            # 这类检查看到的是空轨迹，无法区分"没调用"和"调用失败"）
            try:
                from app.agent.trace import get_current_trace
                ct = get_current_trace()
                if ct is not None:
                    ct.add_tool_call(
                        name=name, params=params,
                        result_summary="",
                        latency_ms=latency, success=False,
                        error=str(exc)[:200],
                    )
            except Exception:
                pass
            return {"success": False, "error": str(exc)}

    def _audit(self, record: ToolCallAudit) -> None:
        self._audit_log.append(record)
        if len(self._audit_log) > 1000:
            self._audit_log = self._audit_log[-500:]

        # 高风险的写审计日志文件
        if record.risk_level == "high":
            # 会话内是否存在已批准的审批上下文（issue #10：供评分卡派生
            # "高风险调用审批覆盖率/未授权调用数"；proxy = 调用时会话有已批准 key）
            approved_ctx = False
            try:
                from app.agent.approval_router import get_current_pending_keys
                approved_ctx = len(get_current_pending_keys()) > 0
            except Exception:
                pass
            audit_dir = Path(__file__).resolve().parent.parent.parent / "agent_data" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / "tool_calls.jsonl").open("a", encoding="utf-8").write(
                json.dumps({
                    "tool": record.tool_name,
                    "params": record.params,
                    "success": record.success,
                    "risk": record.risk_level,
                    "approved": approved_ctx,
                    "time": record.timestamp,
                }, ensure_ascii=False) + "\n"
            )

    def get_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{
            "tool": r.tool_name, "success": r.success,
            "latency_ms": r.latency_ms, "risk": r.risk_level,
            "error": r.error, "time": r.timestamp,
        } for r in self._audit_log[-limit:]]

    def get_tool_stats(self) -> dict[str, Any]:
        """工具调用统计。"""
        total = len(self._audit_log)
        successes = sum(1 for r in self._audit_log if r.success)
        high_risk = sum(1 for r in self._audit_log if r.risk_level == "high")
        by_tool: dict[str, int] = {}
        for r in self._audit_log:
            by_tool[r.tool_name] = by_tool.get(r.tool_name, 0) + 1

        return {
            "total_calls": total,
            "success_rate": round(successes / total * 100, 1) if total else 0,
            "high_risk_calls": high_risk,
            "by_tool": by_tool,
        }
