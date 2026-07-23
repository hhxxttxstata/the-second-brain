"""LangChain tools — 两层分层架构。

vault/       → 只读工具（Obsidian 知识资产，人写的）
agent_data/  → 读写工具（Agent 运行数据，机器读写）
外部数据     → 实时工具（基金/GitHub/AI 新闻）
"""
from __future__ import annotations

from langchain_core.tools import tool

from app.obsidian import vault
from app.agent import agent_data_service as ads


# ===========================================================================
# Layer 1: vault — 只读工具（Obsidian 知识资产）
# ===========================================================================

@tool
def search_vault(keyword: str, folder: str | None = None) -> str:
    """全文搜索 Obsidian vault 中的笔记/日记。

    vault 是用户的知识资产库，只读。
    Args:
        keyword: 搜索关键词
        folder: 限定范围：diaries/notes/habbits/知识沉淀/Clippings
    """
    return vault.search_notes(keyword, folder=folder)


@tool
def read_folder(folder: str, max_files: int = 10) -> str:
    """读取 Obsidian vault 中某个文件夹的笔记内容。

    Args:
        folder: diaries/notes/habbits/知识沉淀/Clippings
        max_files: 最多文件数
    """
    return vault.read_folder(folder, max_files=max_files)


@tool
def read_file(path: str) -> str:
    """读取 vault 中的一个特定文件。

    Args:
        path: 相对路径，如 "notes/ai-agent-design.md"
    """
    return vault.read_file(path)


@tool
def get_user_profile() -> str:
    """从 vault 读取用户档案 claude.md。"""
    return vault.get_user_profile()


@tool
def vault_structure() -> str:
    """列出 vault 的文件夹结构。"""
    return vault.vault_structure()


# ===========================================================================
# Layer 2: agent_data — 读写工具（Agent 运行数据）
# ===========================================================================

@tool
def read_memory(memory_type: str = "episodic") -> str:
    """读取 Agent 记忆。

    Args:
        memory_type: "stable_profile" | "episodic" | "task"
    """
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


@tool
def write_episodic_memory(content: str, tags: str = "") -> str:
    """写入一条情景记忆到 agent_data（自动管理）。

    当你了解到用户的偏好、近期经历、重要反馈时使用。
    这些会被自动纳入后续对话的上下文中。

    Args:
        content: 记忆内容（1-2 句话）
        tags: 逗号分隔的标签
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    ads.add_episodic(content, tags=tag_list)
    return f"✅ 已记忆: {content[:80]}"


@tool
def update_task_status(task_title: str, status: str = "done") -> str:
    """更新或添加一条任务/待办的状态。

    Args:
        task_title: 任务标题
        status: "done" | "in_progress" | "pending" | "cancelled"
    """
    data = ads.read_memory("task")
    if "todos" not in data:
        data["todos"] = []

    # 查找是否已有
    for t in data["todos"]:
        if t.get("title") == task_title:
            t["status"] = status
            break
    else:
        data["todos"].append({"title": task_title, "status": status, "priority": "medium"})

    ads.write_memory("task", data, merge=False)
    return f"✅ 任务「{task_title}」标记为 {status}"


@tool
def get_today_state() -> str:
    """读取今日状态（计划完成情况等）。"""
    state = ads.read_today_state()
    if not state:
        return "今日尚无状态记录。"
    lines = []
    for k, v in state.items():
        if not k.startswith("__"):
            lines.append(f"- {k}: {v}")
    return "\n".join(lines) if lines else "今日尚无状态记录。"


# ===========================================================================
# Layer 3: 实时外部数据
# ===========================================================================

@tool
def get_fund_data(fund_codes: str = "000001,161725") -> str:
    """获取中国开放式基金实时净值。"""
    try:
        import akshare as ak
        df = ak.fund_open_fund_daily_em()
        if df is None or df.empty:
            return "暂无基金数据。"
        codes = [c.strip().zfill(6) for c in fund_codes.split(",") if c.strip()]
        df_cols = {str(c): c for c in df.columns}
        code_col = next((c for c in df_cols if "代码" in c), df.columns[0])
        name_col = next((c for c in df_cols if "简称" in c or "名称" in c), df.columns[1])
        nav_col = next((c for c in df_cols if "单位净值" in c and "前" not in c), None)
        acc_nav_col = next((c for c in df_cols if "累计净值" in c and "前" not in c), None)
        growth_col = next((c for c in df_cols if "增长率" in c or "涨跌幅" in c), None)
        date_col = next((c for c in df_cols if "日期" in c), None)
        df[code_col] = df[code_col].astype(str).str.zfill(6)
        matched = df[df[code_col].isin(codes)]
        if matched.empty:
            return f"未找到基金代码: {fund_codes}"
        lines = [f"## 基金净值 ({matched[date_col].iloc[0] if date_col else ''})\n"]
        for _, row in matched.iterrows():
            code = str(row[code_col]).zfill(6)
            name = row.get(name_col, code)
            p = [f"\n### {name}（{code}）"]
            if nav_col: p.append(f"  单位净值: {row.get(nav_col, '-')}")
            if acc_nav_col: p.append(f"  累计净值: {row.get(acc_nav_col, '-')}")
            if growth_col: p.append(f"  日增长率: {row.get(growth_col, '-')}%")
            lines.extend(p)
        return "\n".join(lines)
    except Exception as exc:
        return f"基金数据失败: {exc}"


@tool
def get_github_trending(language: str = "", since: str = "weekly") -> str:
    """获取 GitHub 热门仓库。

    Args:
        language: 编程语言过滤，如 'python'
        since: 'daily'/'weekly'/'monthly'
    """
    import httpx
    try:
        from datetime import datetime, timedelta, timezone
        days = {"daily": 1, "weekly": 7, "monthly": 30}.get(since, 7)
        d = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"https://api.github.com/search/repositories?q=created:>{d}+stars:>100&sort=stars&order=desc&per_page=10"
        if language: url += f"+language:{language}"
        resp = httpx.get(url, headers={"Accept": "application/vnd.github.v3+json"}, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("items", [])[:10]
        if not items: return "暂无热门仓库。"
        lang_label = f" ({language})" if language else ""
        lines = [f"## 热门 GitHub{lang_label}（{since}）\n"]
        for i, r in enumerate(items, 1):
            lines.append(f"{i}. [{r.get('stargazers_count',0)}★] {r.get('full_name','?')}")
            lines.append(f"   {(r.get('description') or '(无描述)')[:120]}")
            lines.append(f"   {r.get('language','?')} | {r.get('html_url','')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"GitHub 趋势失败: {exc}"


@tool
def get_ai_news(max_items: int = 5) -> str:
    """AI 行业动态（arXiv + GitHub releases）。"""
    import httpx
    from datetime import datetime, timedelta, timezone
    items: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    try:
        query = "+OR+".join(['all:"LLM"','all:"AI agent"','all:"RAG"','all:"foundation model"'])
        resp = httpx.get(f"http://export.arxiv.org/api/query?search_query={query}&start=0&max_results=7&sortBy=submittedDate&sortOrder=descending", timeout=15)
        if resp.status_code == 200:
            import feedparser
            for entry in feedparser.parse(resp.text).entries[:max_items]:
                t = getattr(entry, "title", "").strip()[:100]
                if t: items.append(f"📄 {t}")
    except Exception: pass
    for owner, repo in [("langchain-ai","langgraph"),("langgenius","dify"),("microsoft","autogen")]:
        try:
            resp = httpx.get(f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=1", headers={"Accept":"application/vnd.github.v3+json"}, timeout=10)
            if resp.status_code == 200 and isinstance(resp.json(), list) and resp.json():
                r = resp.json()[0]
                pub = r.get("published_at","")
                if pub and datetime.fromisoformat(pub.replace("Z","+00:00")) > cutoff:
                    items.append(f"🚀 {owner}/{repo}: {r.get('name',r.get('tag_name',''))}")
        except Exception: pass
    return "## AI 行业动态\n\n" + ("\n".join(items[:max_items+3]) if items else "暂无最新动态。")


# ===========================================================================
# Layer 4: 搜索互联网
# ===========================================================================

@tool
def search_web(query: str, max_results: int = 5) -> str:
    """搜索互联网获取最新信息。当你不知道答案、需要最新数据时使用。

    Args:
        query: 搜索关键词（中文/英文均可）
        max_results: 返回结果条数
    """
    from ddgs import DDGS
    import warnings; warnings.filterwarnings('ignore')

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region='cn-zh', max_results=max_results))

        if not results:
            return f"搜索「{query}」未找到结果。"

        lines = [f"## 搜索: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "").strip()
            body = r.get("body", "").strip()
            href = r.get("href", "")
            if not title:
                continue
            lines.append(f"{i}. **{title[:100]}**")
            if body:
                lines.append(f"   {body[:200]}")
            if href:
                lines.append(f"   🔗 {href[:120]}")
        return "\n".join(lines)

    except Exception as exc:
        return f"搜索失败: {exc}"


# ===========================================================================
# 完整工具列表
# ===========================================================================

AGENT_TOOLS = [
    # Layer 1: vault (read-only)
    search_vault, read_folder, read_file, get_user_profile, vault_structure,
    # Layer 2: agent_data (read-write)
    read_memory, write_episodic_memory, update_task_status, get_today_state,
    # Layer 3: external data
    get_fund_data, get_github_trending, get_ai_news,
    # Layer 4: internet search
    search_web,
]
