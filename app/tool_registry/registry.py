"""Agent 工具生态 — 统一的 Native + MCP Tool Registry。

架构:
─────────────────────────────────────────────────────
  Tools (agent 看到的扁平列表)
  ├── @tool search_web           (Native)
  ├── @tool search_vault         (Native)
  ├── @tool write_memory         (Native)
  └── @mcp_tool github_mcp/...   (MCP 服务器)
─────────────────────────────────────────────────────
  Tool Registry
  ├── native/   → 纯 Python 函数
  └── mcp/      → MCP 服务器 (stdio/HTTP)
                     ├── github_mcp
                     ├── filesystem_mcp
                     ├── browser_mcp
                     └── calendar_mcp
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
    source: str                  # "native" | "mcp"
    server_name: str | None      # MCP server name if MCP
    risk_level: str              # "low" | "medium" | "high"
    side_effects: list[str]      # 副作用描述（审计用）
    handler: Any = None          # native callable / MCP server ref


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
# MCP 服务器管理器
# ============================================================================

@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    risk_level: str = "low"
    side_effects: list[str] = field(default_factory=list)


BUILTIN_MCP_SERVERS: list[MCPServerConfig] = [
    MCPServerConfig(
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        description="GitHub API — 仓库、Issue、PR、代码搜索",
        risk_level="medium",
        side_effects=["读取 GitHub 公开/私有仓库", "可能触发 API 调用"],
    ),
    MCPServerConfig(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "D:/MYWORLD", "D:/MyAgent/agent_data"],
        description="安全的文件系统访问 — 只读 vault + 读写 agent_data",
        risk_level="medium",
        side_effects=["读取 vault 文件", "写入 agent_data 目录"],
    ),
    MCPServerConfig(
        name="browser",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-puppeteer"],
        description="无头浏览器 — 截图网页、提取内容",
        risk_level="high",
        side_effects=["打开外部网页", "可能下载资源"],
    ),
    MCPServerConfig(
        name="calendar",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-google-calendar"],
        description="Google Calendar — 读取/创建日程事件",
        risk_level="medium",
        side_effects=["读取日程", "创建/修改事件"],
    ),
]


class MCPServerProcess:
    """一个 MCP 服务器进程的句柄（持久连接）。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._read: Any | None = None
        self._write: Any | None = None
        self._session: Any | None = None
        self._tools: list[dict[str, Any]] = []
        self._ready = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    async def start(self) -> bool:
        """启动 MCP 服务器进程并保持连接。"""
        try:
            from mcp.client.stdio import StdioServerParameters, stdio_client
            from mcp.client.session import ClientSession

            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env or None,
            )

            # store client ref so it stays alive
            self._client_ctx = stdio_client(params)
            self._read, self._write = await self._client_ctx.__aenter__()
            self._session = await ClientSession(self._read, self._write).__aenter__()
            await self._session.initialize()

            result = await self._session.list_tools()
            self._tools = []
            for t in result.tools:
                d = t.model_dump() if hasattr(t, 'model_dump') else dict(t)
                self._tools.append(d)

            self._ready = True
            logger.info("mcp_server_ready", name=self.name, tools=len(self._tools))
            return True
        except Exception as exc:
            logger.warning("mcp_server_start_failed", name=self.name, error=str(exc))
            return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过持久连接调用工具。"""
        try:
            if not self._session:
                return {"success": False, "error": "MCP 服务器未就绪"}
            result = await self._session.call_tool(tool_name, arguments)
            content = result.content if hasattr(result, 'content') else []
            return {"success": True, "content": content}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def stop(self) -> None:
        """关闭持久连接。"""
        try:
            if self._session:
                await self._session.__aexit__(None, None, None)
            if self._client_ctx:
                await self._client_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._ready = False


# ============================================================================
# 统一的 Tool Registry
# ============================================================================

class ToolRegistry:
    """所有工具的注册中心。统一 Native 和 MCP 工具。"""

    def __init__(self) -> None:
        self._native_tools: dict[str, RegisteredTool] = {}
        self._mcp_servers: dict[str, MCPServerProcess] = {}
        self._audit_log: list[ToolCallAudit] = []
        self._started = False

    # ── Native 工具注册 ──

    def register_native(self, tool: RegisteredTool) -> None:
        assert tool.source == "native" and tool.handler is not None
        self._native_tools[tool.name] = tool
        logger.info("tool_registered", name=tool.name, source="native",
                     risk=tool.risk_level)

    # ── MCP 服务器注册 ──

    def register_mcp_server(self, config: MCPServerConfig) -> None:
        """注册一个 MCP 服务器（但直到 start() 才启动进程）。"""
        proc = MCPServerProcess(config)
        self._mcp_servers[config.name] = proc
        logger.info("mcp_registered", name=config.name)

    async def start(self) -> int:
        """启动所有 MCP 服务器并发现工具。"""
        count = 0
        for server in self._mcp_servers.values():
            ok = await server.start()
            if ok:
                for t in server.tools:
                    name = f"{server.name}_{t.get('name', '?')}"
                    schema = t.get("inputSchema", t.get("input_schema", {}))
                    desc = t.get("description", server.config.description)
                    self._register_mcp_tool(name, desc, schema, server.name,
                                            server.config.risk_level,
                                            server.config.side_effects)
                    count += 1
        self._started = True
        logger.info("registry_ready", native=len(self._native_tools),
                     mcp=count)
        return count

    def _register_mcp_tool(self, name: str, description: str,
                           schema: dict[str, Any], server_name: str,
                           risk_level: str, side_effects: list[str]) -> None:
        """注册一个从 MCP 发现的工具。"""
        if name in self._native_tools:
            name = f"mcp_{name}"  # 防止重名
        self._native_tools[name] = RegisteredTool(
            name=name,
            description=f"[MCP/{server_name}] {description}",
            schema_=schema,
            source="mcp",
            server_name=server_name,
            risk_level=risk_level,
            side_effects=side_effects,
        )

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
        """执行任意工具（native / MCP 统一入口）。"""
        tool = self._native_tools.get(name)
        if not tool:
            return {"success": False, "error": f"未知工具: {name}"}

        start = time.monotonic()
        try:
            if tool.source == "native":
                result = tool.handler(**params)
            elif tool.source == "mcp" and tool.server_name:
                server = self._mcp_servers.get(tool.server_name)
                if not server:
                    return {"success": False, "error": f"MCP 服务器 {tool.server_name} 未就绪"}
                # MCP 工具名是原始名（不带 server_ 前缀）
                mcp_tool_name = name[len(tool.server_name) + 1:]
                result = await server.call_tool(mcp_tool_name, params)
            else:
                return {"success": False, "error": f"工具 {name} 类型错误"}

            latency = int((time.monotonic() - start) * 1000)
            result_text = str(result)[:200]
            self._audit(ToolCallAudit(
                tool_name=name, params=params, result_summary=result_text,
                latency_ms=latency, success=True, error=None,
                risk_level=tool.risk_level,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            ))
            return {"success": True, "result": result}

        except Exception as exc:
            latency = int((time.monotonic() - start) * 1000)
            self._audit(ToolCallAudit(
                tool_name=name, params=params, result_summary="",
                latency_ms=latency, success=False, error=str(exc),
                risk_level=tool.risk_level,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            ))
            return {"success": False, "error": str(exc)}

    def _audit(self, record: ToolCallAudit) -> None:
        self._audit_log.append(record)
        if len(self._audit_log) > 1000:
            self._audit_log = self._audit_log[-500:]

        # 高风险的写审计日志文件
        if record.risk_level == "high":
            audit_dir = Path(__file__).resolve().parent.parent.parent / "agent_data" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / "tool_calls.jsonl").open("a", encoding="utf-8").write(
                json.dumps({
                    "tool": record.tool_name,
                    "params": record.params,
                    "success": record.success,
                    "risk": record.risk_level,
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
