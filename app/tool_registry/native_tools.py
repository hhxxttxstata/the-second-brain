"""Native tools for Tool Registry — 注册所有内置工具到 registry。"""
from __future__ import annotations

import json
from typing import Any

from app.core.logging import logger
from app.tool_registry.registry import RegisteredTool, ToolRegistry


def register_all_native_tools(registry: ToolRegistry) -> None:
    """注册所有内置工具到 registry。"""

    # ── vault 只读工具 ──

    from app.obsidian import vault

    def _search_vault(**kw: Any) -> str:
        return vault.search_notes(**kw)

    registry.register_native(RegisteredTool(
        name="search_vault",
        description="全文搜索 Obsidian vault 中的笔记/日记",
        schema_={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "folder": {"type": "string", "description": "限定范围：diaries/notes/habbits/知识沉淀"},
            },
            "required": ["keyword"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_search_vault,
    ))

    def _read_folder(**kw: Any) -> str:
        return vault.read_folder(**kw)

    registry.register_native(RegisteredTool(
        name="read_folder",
        description="读取 vault 中某个文件夹的全部笔记",
        schema_={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "文件夹名"},
                "max_files": {"type": "integer", "description": "最多读几篇"},
            },
            "required": ["folder"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_read_folder,
    ))

    def _read_file(**kw: Any) -> str:
        # schema 暴露的参数名是 path，vault.read_file 实际签名是 rel_path
        rel_path = kw.get("path") or kw.get("rel_path", "")
        return vault.read_file(rel_path)

    registry.register_native(RegisteredTool(
        name="read_file",
        description="读取 vault 中一个特定文件",
        schema_={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "如 notes/ai-agent-design.md"},
            },
            "required": ["path"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_read_file,
    ))

    # ── vault 写工具（HITL 审批） ──

    def _vault_append(**kw: Any) -> str:
        return vault.append_to_file(**kw)

    registry.register_native(RegisteredTool(
        name="vault_append",
        description="向 Obsidian 笔记文件追加内容",
        schema_={
            "type": "object",
            "properties": {
                "rel_path": {"type": "string", "description": "相对路径, 如 diaries/2026-07-28.md"},
                "content": {"type": "string", "description": "要追加的内容"},
            },
            "required": ["rel_path", "content"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["修改 Obsidian 文件"],
        handler=_vault_append,
    ))

    def _vault_write(**kw: Any) -> str:
        return vault.write_file(**kw)

    registry.register_native(RegisteredTool(
        name="vault_write",
        description="覆盖写入 Obsidian 笔记文件",
        schema_={
            "type": "object",
            "properties": {
                "rel_path": {"type": "string", "description": "相对路径"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["rel_path", "content"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["覆盖修改 Obsidian 文件"],
        handler=_vault_write,
    ))

    # ── agent_data 读写工具 ──

    from app.agent import agent_data_service as ads

    def _read_memory(**kw: Any) -> str:
        memory_type = kw.pop("memory_type", "episodic")
        data = ads.read_memory(memory_type)
        if not data:
            return "(暂无记忆)"
        if memory_type == "stable_profile":
            return ads.format_profile()
        elif memory_type == "episodic":
            return ads.format_episodic(limit=20)
        elif memory_type == "task":
            return ads.format_tasks()
        return str(data)[:500]

    registry.register_native(RegisteredTool(
        name="read_memory",
        description="读取 Agent 记忆（stable_profile/episodic/task）",
        schema_={
            "type": "object",
            "properties": {
                "memory_type": {"type": "string", "description": "stable_profile/episodic/task"},
            },
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_read_memory,
    ))

    def _search_memories(**kw: Any) -> str:
        """搜索记忆 — 按关键词模糊搜索所有记忆条目。"""
        from .memory_store import search_memories as sql_search
        query = kw.get("query", "")
        limit = int(kw.get("limit", 5))
        memory_type = kw.get("memory_type", "")
        if not query:
            return "请输入搜索关键词 (query)"
        results = sql_search(query=query, limit=limit,
                             memory_type=memory_type or None)
        if not results:
            return f"未找到与「{query}」相关的记忆。"
        lines = [f"## 记忆搜索: {query}\n"]
        for r in results:
            ts = r.get("created_at", "")[:10]
            mtype = r.get("memory_type", "?")
            content = r.get("content", "")[:200]
            lines.append(f"- [{ts}] [{mtype}] {content}")
        return "\n".join(lines)

    registry.register_native(RegisteredTool(
        name="search_memories",
        description="搜索 Agent 记忆（按关键词模糊搜索所有记忆类型）",
        schema_={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "limit": {"type": "integer", "description": "返回条数（默认5）"},
                "memory_type": {"type": "string", "description": "可选过滤：episodic/task/task_plan"},
            },
            "required": ["query"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_search_memories,
    ))

    def _write_episodic(**kw: Any) -> str:
        content = kw.get("content", "")
        tags_str = kw.get("tags", "")
        tag_list = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
        ads.add_episodic(content, tags=tag_list)
        return f"✅ 已记忆: {content[:80]}"

    registry.register_native(RegisteredTool(
        name="write_memory",
        description="写入一条情景记忆到 agent_data（记住用户的偏好/事实/经历）",
        schema_={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "tags": {"type": "string", "description": "逗号分隔标签"},
            },
            "required": ["content"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["写入记忆 JSON 文件"],
        handler=_write_episodic,
    ))

    def _update_task(**kw: Any) -> str:
        title = kw.get("task_title", "")
        status = kw.get("status", "done")
        data = ads.read_memory("task")
        if "todos" not in data:
            data["todos"] = []
        for t in data["todos"]:
            if t.get("title") == title:
                t["status"] = status
                break
        else:
            data["todos"].append({"title": title, "status": status, "priority": "medium"})
        ads.write_memory("task", data, merge=False)
        return f"✅ 任务「{title}」已更新为 {status}"

    registry.register_native(RegisteredTool(
        name="update_task_status",
        description="更新任务状态（done/in_progress/pending/cancelled）",
        schema_={
            "type": "object",
            "properties": {
                "task_title": {"type": "string", "description": "任务标题"},
                "status": {"type": "string", "description": "done/in_progress/pending"},
            },
            "required": ["task_title", "status"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["修改 task memory"],
        handler=_update_task,
    ))

    # ── 互联网搜索 ──

    from ddgs import DDGS
    import warnings

    def _search_web(**kw: Any) -> str:
        query = kw.get("query", "")
        max_results = kw.get("max_results", 5)
        warnings.filterwarnings("ignore")
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, region="cn-zh", max_results=max_results))
            if not results:
                return f"搜索「{query}」无结果。"
            lines = [f"## 搜索: {query}\n"]
            for i, r in enumerate(results, 1):
                title = r.get("title", "").strip()
                body = r.get("body", "").strip()
                href = r.get("href", "")
                if title:
                    lines.append(f"{i}. **{title[:100]}**")
                    if body:
                        lines.append(f"   {body[:200]}")
                    if href:
                        lines.append(f"   🔗 {href[:120]}")
            return "\n".join(lines)
        except Exception as exc:
            return f"搜索失败: {exc}"

    registry.register_native(RegisteredTool(
        name="search_web",
        description="搜索互联网获取最新信息。当你不知道答案、需要最新数据时用",
        schema_={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "description": "返回条数"},
            },
            "required": ["query"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["访问外部搜索引擎"],
        handler=_search_web,
    ))

    # ── 基金数据 ──
    from app.agent.graphs.tools_legacy import get_fund_data, get_github_trending, get_ai_news

    def _fund(**kw: Any) -> str:
        from app.agent.graphs.tools_legacy import get_fund_data as gf
        return gf.invoke(kw)

    registry.register_native(RegisteredTool(
        name="get_fund_data",
        description="获取中国开放式基金实时净值",
        schema_={
            "type": "object",
            "properties": {
                "fund_codes": {"type": "string", "description": "基金代码逗号分隔，如 000001,161725"},
            },
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["请求外部 API"],
        handler=_fund,
    ))

    def _github(**kw: Any) -> str:
        from app.agent.graphs.tools_legacy import get_github_trending as gg
        return gg.invoke(kw)

    registry.register_native(RegisteredTool(
        name="get_github_trending",
        description="获取 GitHub 热门仓库",
        schema_={
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "编程语言"},
                "since": {"type": "string", "description": "daily/weekly/monthly"},
            },
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["请求 GitHub API"],
        handler=_github,
    ))

    def _news(**kw: Any) -> str:
        from app.agent.graphs.tools_legacy import get_ai_news as gn
        return gn.invoke(kw)

    registry.register_native(RegisteredTool(
        name="get_ai_news",
        description="AI 行业动态（arXiv 论文 + GitHub releases）",
        schema_={
            "type": "object",
            "properties": {
                "max_items": {"type": "integer", "description": "返回条数"},
            },
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["请求 arXiv + GitHub API"],
        handler=_news,
    ))

    logger.info("native_tools_registered", count=len(registry._native_tools))

    # ── Topic Memory 工具 ──

    from app.agent.topic_memory import (
        read_topic as _read_topic,
        write_topic as _write_topic,
        search_topic as _search_topic,
        upsert_index_entry,
        load_relevant_memories,
        read_index,
        get_all_topics,
    )

    def _do_read_topic(**kw: Any) -> str:
        path = kw.get("topic_path", "")
        return _read_topic(path)

    registry.register_native(RegisteredTool(
        name="read_topic_memory",
        description="读取一个 Topic Memory 文件的完整内容。Topic 文件在 agent_data/memory/ 下，如 people/tata、preferences、projects/2027-autumn-recruitment",
        schema_={
            "type": "object",
            "properties": {
                "topic_path": {"type": "string", "description": "Topic 文件路径（不含 .md），如 'people/tata'、'preferences'、'projects/2027-autumn-recruitment'"},
            },
            "required": ["topic_path"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_do_read_topic,
    ))

    def _do_write_topic(**kw: Any) -> str:
        path = kw.get("topic_path", "")
        content = kw.get("content", "")
        line = _write_topic(path, content)
        return f"✅ 已写入 topic memory: {line}"

    registry.register_native(RegisteredTool(
        name="write_topic_memory",
        description="写入一个 Topic Memory 文件（覆盖写）。Topic 文件在 agent_data/memory/ 下，如 'people/zhang-san' 会创建 people/zhang-san.md，'preferences' 会创建 preferences.md。写入后自动在 MEMORY.md 更新索引。",
        schema_={
            "type": "object",
            "properties": {
                "topic_path": {"type": "string", "description": "Topic 文件路径（不含 .md），如 'people/tata'、'preferences'"},
                "content": {"type": "string", "description": "Markdown 内容"},
            },
            "required": ["topic_path", "content"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["写入 memory Topic File"],
        handler=_do_write_topic,
    ))

    def _do_search_topic(**kw: Any) -> str:
        query = kw.get("query", "")
        results = _search_topic(query)
        if not results:
            return f"未找到与「{query}」相关的 topic memory。"
        lines = [f"## Topic 搜索: {query}\n"]
        for r in results:
            lines.append(f"- [{r['file']}:{r['line']}] {r['content']}")
        return "\n".join(lines)

    registry.register_native(RegisteredTool(
        name="search_topic_memory",
        description="在所有 Topic Memory 文件中搜索关键词",
        schema_={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=[],
        handler=_do_search_topic,
    ))
