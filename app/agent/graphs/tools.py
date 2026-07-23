"""LangChain tools — adapter layer over the new Tool Registry.

graph tools.py → 工具代理（不直接写逻辑，统一走 ToolRegistry）
"""
from __future__ import annotations

import functools
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
    """启动所有 MCP 服务器并注册工具。（在同步上下文中调用）"""
    import asyncio
    reg = get_registry()
    if reg._started:
        return len([t for t in reg.list_tools_for_llm() if "[MCP/" in t.get("description", "")])
    from app.tool_registry.registry import BUILTIN_MCP_SERVERS
    for cfg in BUILTIN_MCP_SERVERS:
        if cfg.name in ("github", "browser"):
            reg.register_mcp_server(cfg)
    try:
        count = asyncio.run(reg.start())
        return count
    except Exception as exc:
        logger.error("mcp_start_failed", error=str(exc))
        return 0


def list_all_tools_for_llm() -> list[dict[str, Any]]:
    """返回给 ChatOpenAI.bind_tools 的工具列表。"""
    return get_registry().list_tools_for_llm()


# ── 异步统一执行器 ──

async def execute_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return await get_registry().execute(name, params)


# ── 生成 LangChain tool list ──

def _build_langchain_tools() -> list:
    """把 ToolRegistry 中的工具转为 LangChain StructuredTool 列表。"""
    registry = get_registry()
    lc_tools = []

    for t_info in registry.list_tools_for_llm():
        name = t_info["name"]
        description = t_info["description"]
        schema = t_info.get("input_schema", {})

        # 生成一个适配器函数
        async def _make_async_fn(t_name: str = name):
            async def fn(**kwargs: Any) -> str:
                result = await registry.execute(t_name, kwargs)
                if result.get("success"):
                    r = result.get("result", "")
                    return str(r) if not isinstance(r, str) else r
                return f"❌ {result.get('error', 'unknown error')}"

            fn.__name__ = t_name
            fn.__doc__ = description
            return fn

        import asyncio
        fn = asyncio.run(_make_async_fn(name))

        lc_tools.append(StructuredTool.from_function(
            func=fn,
            name=name,
            description=description,
            args_schema=None,  # schema 在 bind_tools 时通过 input_schema 传
        ))

    return lc_tools


# ── 兼容当前代码的同步 wrapper ──

def _sync_execute(name: str, **kwargs: Any) -> str:
    """同步执行工具（用于当前 graph 代码）。"""
    import asyncio
    result = asyncio.run(execute_tool(name, kwargs))
    if result.get("success"):
        r = result.get("result", "")
        return str(r) if not isinstance(r, str) else r
    return f"❌ {result.get('error', 'unknown error')}"


# ── 暴露给 graph 使用的工具列表 ──

# 先用同步版 wrapper 保持兼容
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


# ── 注册所有 native 工具后生成列表 ──
AGENT_TOOLS = [
    search_vault, read_folder, read_file,
    read_memory, write_memory, update_task_status,
    search_web,
    get_fund_data, get_github_trending, get_ai_news,
]
