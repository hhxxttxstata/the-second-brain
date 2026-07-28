"""LangChain tools — adapter layer over the new Tool Registry.

graph tools.py → 工具代理（不直接写逻辑，统一走 ToolRegistry）

工具列表现在从 ToolRegistry 动态生成，同时包含 native + MCP 工具。
"""
from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import tool
from langchain_core.tools.structured import StructuredTool

from app.core.logging import logger
from app.tool_registry.registry import ToolRegistry, MCPServerConfig

# 全局 registry
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        # 注册 native 工具
        from app.tool_registry.native_tools import register_all_native_tools
        register_all_native_tools(_registry)
    return _registry


def ensure_mcp_started() -> int:
    """启动所有 MCP 服务器并注册工具到 registry，逐个日志 + 独立容错。

    MCP 工具注册逻辑在 reg.start() 中（_register_mcp_tool），
    这里逐个启动、独立 try/except 确保单点故障不阻塞整体。
    """
    import asyncio
    reg = get_registry()
    if reg._started:
        mcp_count = len([t for t in reg.list_tools_for_llm()
                         if "[MCP/" in t.get("description", "")])
        logger.info("mcp.already_started", count=mcp_count)
        return mcp_count

    from app.tool_registry.registry import BUILTIN_MCP_SERVERS
    for cfg in BUILTIN_MCP_SERVERS:
        if cfg.name in ("github", "browser"):
            logger.info("mcp.register", server=cfg.name,
                        command=cfg.command, args=cfg.args)
            reg.register_mcp_server(cfg)

    # 逐个启动，reg.start() 内部负责 _register_mcp_tool
    started = 0
    for name, server in list(reg._mcp_servers.items()):
        logger.info("mcp.starting", server=name)
        try:
            # reg.start() 对单个 server 调用 server.start()
            # 但我们只需对当前 server 调用
            ok = asyncio.run(server.start())
            if ok:
                # 注册该 server 的工具到 registry
                for t in server.tools:
                    t_name = f"{server.name}_{t.get('name', '?')}"
                    schema = t.get("inputSchema", t.get("input_schema", {}))
                    desc = t.get("description", server.config.description)
                    reg._register_mcp_tool(
                        t_name, desc, schema, server.name,
                        server.config.risk_level,
                        server.config.side_effects,
                    )
                    started += 1
                tool_names = [t.get("name", "?") for t in server.tools]
                logger.info("mcp.started", server=name,
                            tools=len(server.tools),
                            tool_names=tool_names[:5])
            else:
                logger.warning("mcp.start_failed", server=name)
        except Exception as exc:
            logger.error("mcp.start_error", server=name,
                         error=str(exc)[:200],
                         error_type=type(exc).__name__)

    reg._started = True
    actual_mcp = len([t for t in reg.list_tools_for_llm()
                      if "[MCP/" in t.get("description", "")])
    logger.info("mcp.summary", total=actual_mcp, started=started,
                servers=len(reg._mcp_servers))
    return actual_mcp


def list_all_tools_for_llm() -> list[dict[str, Any]]:
    """返回给 ChatOpenAI.bind_tools 的工具列表。"""
    return get_registry().list_tools_for_llm()


# ── 异步统一执行器 ──

async def execute_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return await get_registry().execute(name, params)


# ── 生成 LangChain tool list ──

def build_agent_tools() -> list:
    """从 ToolRegistry 动态生成完整的工具列表（native + MCP）。

    每工具有独立日志，失败不阻塞整体。
    """
    registry = get_registry()
    lc_tools: list = []
    native_count = 0
    mcp_count = 0
    failed = 0

    for t_info in registry.list_tools_for_llm():
        name = t_info["name"]
        description = t_info.get("description", "")
        schema = t_info.get("input_schema", {})
        source = "MCP" if "[MCP/" in description else "native"

        # 同步适配器函数 — 由 StructuredTool 同步执行
        def _make_sync_fn(t_name: str = name, t_desc: str = description):
            def fn(**kwargs: Any) -> str:
                try:
                    result = asyncio.run(execute_tool(t_name, kwargs))
                    if result.get("success"):
                        r = result.get("result", "")
                        return str(r) if not isinstance(r, str) else r
                    return f"❌ {result.get('error', 'unknown error')}"
                except Exception as exc:
                    logger.error("tool_exec_failed", tool=t_name, error=str(exc)[:200])
                    return f"❌ {exc}"

            fn.__name__ = t_name
            fn.__doc__ = t_desc
            return fn

        try:
            sync_fn = _make_sync_fn()
            lc_tools.append(StructuredTool.from_function(
                func=sync_fn,
                name=name,
                description=description,
                args_schema=None,
            ))
            if source == "MCP":
                mcp_count += 1
            else:
                native_count += 1
        except Exception as exc:
            failed += 1
            logger.error("tool.build_failed", tool=name,
                         error=str(exc)[:200], source=source)

    logger.info("tools.build_complete",
                native=native_count, mcp=mcp_count,
                failed=failed, total=len(lc_tools))

    return lc_tools


# ── 兼容当前代码的同步 wrapper ──

def _sync_execute(name: str, **kwargs: Any) -> str:
    """同步执行工具（用于当前 graph 代码）。"""
    result = asyncio.run(execute_tool(name, kwargs))
    if result.get("success"):
        r = result.get("result", "")
        return str(r) if not isinstance(r, str) else r
    return f"❌ {result.get('error', 'unknown error')}"


# ── 暴露给 graph 使用的工具列表（保留 @tool 装饰器独立函数以保持 IDE 类型推断） ──

@tool
def search_vault(keyword: str, folder: str | None = None) -> str:
    """全文搜索 Obsidian vault 中的笔记/日记。"""
    return _sync_execute("search_vault", keyword=keyword, folder=folder)


@tool
def read_folder(folder: str, max_files: int = 10) -> str:
    """读取 vault 中某个文件夹的全部笔记。"""
    return _sync_execute("read_folder", folder=folder, max_files=max_files)


@tool
def read_file(path: str) -> str:
    """读取 vault 中一个特定文件。"""
    return _sync_execute("read_file", path=path)


@tool
def read_memory(memory_type: str = "episodic") -> str:
    """读取 Agent 记忆（stable_profile/episodic/task）。"""
    return _sync_execute("read_memory", memory_type=memory_type)


@tool
def write_memory(content: str, tags: str = "") -> str:
    """写入一条情景记忆。"""
    return _sync_execute("write_memory", content=content, tags=tags)


@tool
def update_task_status(task_title: str, status: str = "done") -> str:
    """更新任务状态。"""
    return _sync_execute("update_task_status", task_title=task_title, status=status)


@tool
def search_web(query: str, max_results: int = 5) -> str:
    """搜索互联网获取最新信息。"""
    return _sync_execute("search_web", query=query, max_results=max_results)


@tool
def get_fund_data(fund_codes: str = "000001,161725") -> str:
    """获取基金实时净值。"""
    return _sync_execute("get_fund_data", fund_codes=fund_codes)


@tool
def get_github_trending(language: str = "", since: str = "weekly") -> str:
    """GitHub 热门仓库。"""
    return _sync_execute("get_github_trending", language=language, since=since)


@tool
def get_ai_news(max_items: int = 5) -> str:
    """AI 行业动态。"""
    return _sync_execute("get_ai_news", max_items=max_items)


# ── 动态生成的完整工具列表 ──

_agent_tools_cache: list | None = None


def get_agent_tools() -> list:
    """懒加载 AGENT_TOOLS，首次调用时触发 MCP 启动 + 动态生成。

    MCP 启动失败不阻塞——确保 native 工具始终可用。
    """
    global _agent_tools_cache
    if _agent_tools_cache is None:
        # 必须先启动 MCP，再构建工具列表
        ensure_mcp_started()
        _agent_tools_cache = build_agent_tools()
    return _agent_tools_cache


def reset_agent_tools_cache() -> None:
    """重置缓存（用于测试 / MCP 重连后）。"""
    global _agent_tools_cache
    _agent_tools_cache = None


# 向后兼容：静态 import 用 get_agent_tools() 获取实际列表
# 旧代码 from .tools import AGENT_TOOLS → 请改用 get_agent_tools()
AGENT_TOOLS: list = []

