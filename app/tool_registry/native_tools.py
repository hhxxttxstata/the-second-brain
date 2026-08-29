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

    def _ask_clarification(**kw: Any) -> str:
        """信息不足时向用户提问澄清（可审计，不猜）。"""
        from app.agent.memory_store import add_memory
        question = kw.get("question", "")
        if not question:
            return "❌ 缺少 question 参数"
        try:
            add_memory(question, memory_type="conversation", tags=["clarification"],
                       importance=2, source="clarification")
        except Exception:
            pass
        return f"❓ 需要向用户澄清: {question}"

    registry.register_native(RegisteredTool(
        name="ask_clarification",
        description="向用户提问澄清。当用户请求信息不足/指代不明（'那个项目'、缺少必要参数）时调用，先问清楚再执行，禁止猜测",
        schema_={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题（具体、单一）"},
            },
            "required": ["question"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["记录一次澄清请求到记忆"],
        handler=_ask_clarification,
    ))

    def _delete_memory(**kw: Any) -> str:
        """删除/作废一条记忆（忘记能力）。"""
        from app.agent.memory_store import _get_conn, search_memories
        query = kw.get("query", "")
        memory_type = kw.get("memory_type")
        memory_id = kw.get("memory_id")
        conn = _get_conn()

        if memory_id is not None:
            row = conn.execute(
                "SELECT id, memory_type, substr(content,1,80) c FROM memories WHERE id=?",
                (int(memory_id),),
            ).fetchone()
            if not row:
                return f"❌ 记忆 #{memory_id} 不存在"
            conn.execute(
                "UPDATE memories SET deprecated=1, superseded_by='user deleted' WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            return f"✅ 已删除记忆 #{row['id']} [{row['memory_type']}] {row['c'][:60]}"

        # 无 id：先搜索候选，让模型用 memory_id 指定
        hits = search_memories(query=query or "", memory_type=memory_type, limit=10)
        if not hits:
            return "没有找到匹配的记忆"
        lines = [f"匹配到 {len(hits)} 条记忆（请用 memory_id 指定要删除的）:"]
        for h in hits:
            lines.append(f"  #{h['id']} [{h['memory_type']}] {str(h['content'])[:80]}")
        return "\n".join(lines)

    registry.register_native(RegisteredTool(
        name="delete_memory",
        description="删除/作废一条 Agent 记忆。用户说'忘掉/删除我之前说的 X'时使用：先用 query 搜索出候选（返回记忆 id），再用 memory_id 删除指定条目",
        schema_={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词（无 memory_id 时必填）"},
                "memory_type": {"type": "string", "description": "可选过滤：episodic/task/conversation 等"},
                "memory_id": {"type": "integer", "description": "要删除的记忆 id（先搜索拿到候选）"},
            },
            "required": ["query"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["作废一条记忆（deprecated）"],
        handler=_delete_memory,
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

    # ── 跨会话任务连续性工具 ──

    def _propose_action(**kw: Any) -> str:
        """登记一个待执行/待审批的操作（pending ledger，幂等）。"""
        from app.agent.pending_ledger import propose_action
        from app.agent.session import get_default_session
        description = kw.get("description", "")
        action_type = kw.get("action_type", "task_op")
        params = kw.get("params", {})
        if not description:
            return "❌ 缺少 description 参数（要执行的操作描述）"
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        rec = propose_action(
            session_id=get_default_session(),
            action_type=action_type,
            description=str(description)[:300],
            params=params if isinstance(params, dict) else None,
        )
        return (f"✅ 已登记待执行操作 [{action_type}]: {str(description)[:80]} "
                f"(key: {rec['idempotency_key'][:16]}...) 用户回复「同意」后执行")

    registry.register_native(RegisteredTool(
        name="propose_action",
        description="登记一个待执行/待审批的操作（幂等，执行前自动查重）。当操作需要用户确认后执行、或本次未完成需要下次继续时使用",
        schema_={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "操作描述（人类可读）"},
                "action_type": {"type": "string", "description": "vault_write/vault_append/task_op/memory_write，默认 task_op"},
                "params": {"type": "object", "description": "执行参数（JSON 对象）"},
            },
            "required": ["description"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["登记待执行操作到 pending_ledger"],
        handler=_propose_action,
    ))

    def _create_handoff(**kw: Any) -> str:
        """创建跨会话任务传递（handoff artifact）。"""
        from app.agent.handoff import create_handoff
        goal = kw.get("goal", "")
        if not goal:
            return "❌ 缺少 goal 参数（任务目标，一句话）"
        pending_params = kw.get("pending_params", {})
        if isinstance(pending_params, str):
            try:
                pending_params = json.loads(pending_params)
            except Exception:
                pending_params = {}
        completed = kw.get("completed") or None
        forbidden = kw.get("forbidden") or None
        rec = create_handoff(
            goal=str(goal)[:200],
            pending_tool=kw.get("pending_tool", ""),
            pending_params=pending_params if isinstance(pending_params, dict) else None,
            completed=completed if isinstance(completed, list) else None,
            next_step=kw.get("next_step", ""),
            forbidden=forbidden if isinstance(forbidden, list) else None,
            requires_approval=bool(kw.get("requires_approval", False)),
        )
        status = "awaiting_approval" if kw.get("requires_approval") else "in_progress"
        return (f"✅ 已创建跨会话任务 {rec['task_id']} ({status}): {str(goal)[:80]}"
                + (f"，待执行工具: {kw.get('pending_tool', '')}" if kw.get("pending_tool") else ""))

    registry.register_native(RegisteredTool(
        name="create_handoff",
        description="创建跨会话任务传递（handoff）。当任务本次会话无法完成、需要下次会话继续时使用；下次会话会自动加载该任务并在上下文中注入",
        schema_={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "任务目标（一句话）"},
                "pending_tool": {"type": "string", "description": "待执行的工具名"},
                "pending_params": {"type": "object", "description": "待执行工具的参数（JSON 对象）"},
                "completed": {"type": "array", "description": "已完成步骤列表", "items": {"type": "string"}},
                "next_step": {"type": "string", "description": "下一步动作描述"},
                "forbidden": {"type": "array", "description": "禁止行为列表", "items": {"type": "string"}},
                "requires_approval": {"type": "boolean", "description": "是否需要用户审批，默认 false"},
            },
            "required": ["goal"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["写入 tasks/ + handoffs/ 任务档案"],
        handler=_create_handoff,
    ))

    def _complete_handoff(**kw: Any) -> str:
        """标记跨会话任务为已完成（移入 completed_tasks.jsonl）。"""
        from app.agent.handoff import complete_handoff
        task_id = kw.get("task_id", "")
        if not task_id:
            return "❌ 缺少 task_id 参数（如 task_001）"
        result = kw.get("result", "")
        ok = complete_handoff(task_id, result=result)
        return f"✅ 已完成任务 {task_id}" if ok else f"❌ 任务 {task_id} 不存在或已在完成列表"

    registry.register_native(RegisteredTool(
        name="complete_handoff",
        description="标记跨会话任务（handoff）为已完成，从 active 移入 completed。任务真正完成时必须调用，否则下次会话会继续看到该任务",
        schema_={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID，如 task_001"},
                "result": {"type": "string", "description": "完成结果摘要（可选）"},
            },
            "required": ["task_id"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["更新 tasks/ 任务档案状态"],
        handler=_complete_handoff,
    ))

    def _update_handoff_status(**kw: Any) -> str:
        """更新跨会话任务状态（不完成）。"""
        from app.agent.handoff import update_handoff_status
        task_id = kw.get("task_id", "")
        if not task_id:
            return "❌ 缺少 task_id 参数（如 task_001）"
        status = kw.get("status", "in_progress")
        next_step = kw.get("next_step", "")
        ok = update_handoff_status(task_id, new_status=status, new_next_step=next_step)
        return f"✅ 已更新任务 {task_id} 状态为 {status}" if ok else f"❌ 任务 {task_id} 不存在"

    registry.register_native(RegisteredTool(
        name="update_handoff_status",
        description="更新跨会话任务（handoff）的状态（in_progress/blocked 等）或下一步动作，不完成时使用",
        schema_={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID，如 task_001"},
                "status": {"type": "string", "description": "in_progress/blocked/approved 等，默认 in_progress"},
                "next_step": {"type": "string", "description": "更新后的下一步动作（可选）"},
            },
            "required": ["task_id"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["更新 tasks/ 任务档案状态"],
        handler=_update_handoff_status,
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

    # ── 编程能力工具（Excel / Visio / 代码执行） ──

    from app.agent.code_runner import generate_excel, control_visio, run_code

    def _generate_excel(**kw: Any) -> str:
        return generate_excel(
            file_path=kw.get("file_path", ""),
            data=kw.get("data", []),
            sheet_name=kw.get("sheet_name", "Sheet1"),
            headers=kw.get("headers"),
        )

    registry.register_native(RegisteredTool(
        name="generate_excel",
        description="生成 Excel 报表——把数据写成 .xlsx 文件（支持表头/多行数据/自动列宽）。用户要求生成表格/Excel/报表时使用。数据格式: [[行1列1, 行1列2], [行2列1, ...]]",
        schema_={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "输出路径（如 秋招准备/面经统计.xlsx，相对路径存到 agent_data/outputs/）"},
                "data": {"type": "array", "description": "二维数据 [[col1, col2], ...]，每行一个数组"},
                "sheet_name": {"type": "string", "description": "工作表名（默认 Sheet1）"},
                "headers": {"type": "array", "description": "表头列表（可选）"},
            },
            "required": ["file_path", "data"],
        },
        source="native", server_name=None,
        risk_level="low", side_effects=["创建 Excel 文件"],
        handler=_generate_excel,
    ))

    def _control_visio(**kw: Any) -> str:
        return control_visio(
            action=kw.get("action", "create_flowchart"),
            file_path=kw.get("file_path", ""),
            shapes=kw.get("shapes", []),
        )

    registry.register_native(RegisteredTool(
        name="control_visio",
        description="控制 Microsoft Visio 画流程图——通过 COM 自动化创建 Visio 图形（矩形/菱形/椭圆/平行四边形）。用户要求画流程图/Visio图时使用。shapes 格式: [{\"text\": \"开始\", \"shape\": \"rectangle|decision|ellipse|parallelogram\", \"x\": 1.0, \"y\": 1.0, \"w\": 2.0, \"h\": 1.0}]",
        schema_={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "create_flowchart（创建）| export_pdf（导出PDF）"},
                "file_path": {"type": "string", "description": "输出 .vsdx 路径（默认 agent_data/outputs/diagram.vsdx）"},
                "shapes": {"type": "array", "description": "流程图形状列表，每个含 text/shape/x/y/w/h"},
            },
            "required": ["action"],
        },
        source="native", server_name=None,
        risk_level="medium", side_effects=["打开 Visio 应用", "创建/修改 .vsdx 文件"],
        handler=_control_visio,
    ))

    def _run_code(**kw: Any) -> str:
        return run_code(
            code=kw.get("code", ""),
            working_dir=kw.get("working_dir", ""),
            timeout=int(kw.get("timeout", 60)),
        )

    registry.register_native(RegisteredTool(
        name="run_code",
        description="执行 Python 代码——在受控环境（agent_data/code_runs/）运行 Agent 生成的代码，返回输出。用于数据处理、文件操作、批量转换等编程任务。用户要求'写代码/脚本处理'时使用",
        schema_={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "完整 Python 代码"},
                "working_dir": {"type": "string", "description": "工作目录（可选，默认 agent_data/code_runs/）"},
                "timeout": {"type": "integer", "description": "超时秒数（默认60）"},
            },
            "required": ["code"],
        },
        source="native", server_name=None,
        risk_level="high", side_effects=["在受控目录执行任意 Python 代码"],
        handler=_run_code,
    ))

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
