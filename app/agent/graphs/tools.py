"""LangChain tools — adapter layer over the new Tool Registry.

graph tools.py → 工具代理（不直接写逻辑，统一走 ToolRegistry）

工具列表从 ToolRegistry 动态生成（全部 Native 工具）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from langchain_core.tools import tool
from langchain_core.tools.structured import StructuredTool

from app.core.logging import logger
from app.tool_registry.registry import ToolRegistry

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


def list_all_tools_for_llm() -> list[dict[str, Any]]:
    """返回给 ChatOpenAI.bind_tools 的工具列表。"""
    return get_registry().list_tools_for_llm()


# ── 异步统一执行器 ──

async def execute_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return await get_registry().execute(name, params)


# ── 生成 LangChain tool list ──

def _schema_to_pydantic(model_name: str, schema: dict) -> type | None:
    """将 JSON Schema 转为 Pydantic model，供 StructuredTool 使用。"""
    if not schema or not isinstance(schema, dict):
        return None
    try:
        from pydantic import BaseModel, Field, create_model

        properties = schema.get("properties", {})
        required = schema.get("required", [])
        fields = {}
        for prop_name, prop in properties.items():
            python_type = str  # default
            json_type = prop.get("type", "string")
            type_map = {
                "string": str, "integer": int, "number": float,
                "boolean": bool, "array": list, "object": dict,
            }
            python_type = type_map.get(json_type, str)
            desc = prop.get("description", "")
            default = ... if prop_name in required else None
            if default is not None:
                fields[prop_name] = (Optional[python_type], Field(default=None, description=desc))
            else:
                fields[prop_name] = (python_type, Field(..., description=desc))

        if fields:
            return create_model(model_name, **fields)
    except Exception:
        pass
    return None


def build_agent_tools() -> list:
    """从 ToolRegistry 动态生成完整的工具列表（全部 Native）。

    每工具有独立日志，失败不阻塞整体。
    """
    registry = get_registry()
    lc_tools: list = []
    native_count = 0
    failed = 0

    for t_info in registry.list_tools_for_llm():
        name = t_info["name"]
        description = t_info.get("description", "")
        schema = t_info.get("input_schema", {})

        # 从 input_schema 动态生成 Pydantic args_schema
        # 这样 StructuredTool 能正确暴露参数给 LLM，而非退化为 kwargs
        args_schema = _schema_to_pydantic(name, schema) if schema else None

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
                args_schema=args_schema,
            ))
            native_count += 1
        except Exception as exc:
            failed += 1
            logger.error("tool.build_failed", tool=name,
                         error=str(exc)[:200])

    logger.info("tools.build_complete",
                native=native_count,
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
    """懒加载 AGENT_TOOLS，首次调用时动态生成。"""
    global _agent_tools_cache
    if _agent_tools_cache is None:
        _agent_tools_cache = build_agent_tools()
    return _agent_tools_cache


def reset_agent_tools_cache() -> None:
    """重置缓存（用于测试）。"""
    global _agent_tools_cache
    _agent_tools_cache = None


# 向后兼容：静态 import 用 get_agent_tools() 获取实际列表
# 旧代码 from .tools import AGENT_TOOLS → 请改用 get_agent_tools()
AGENT_TOOLS: list = []

