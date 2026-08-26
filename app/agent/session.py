"""会话管理 — 持久化多轮对话消息 + 自动摘要写入情景记忆。

每轮对话的消息保存到 SQLite messages 表，
下次调用时从 SQLite 加载并注入到 MessagesState。

同时，每 3 轮批量摘要一次，写入 episodic 记忆作为跨会话的长期保留。
"""
from __future__ import annotations

import json
from datetime import date

from .memory_store import save_message, get_session_messages


def get_default_session() -> str:
    """当日默认会话 ID。一天一个会话。"""
    return f"session_{date.today().isoformat()}"


def load_messages(session_id: str,
                  max_turns: int = 20) -> list[dict[str, str]]:
    """加载会话历史消息。"""
    return get_session_messages(session_id, limit=max_turns)


def save_messages(session_id: str,
                  messages: list[dict[str, str]],
                  max_turns: int = 50) -> None:
    """保存会话消息（全量覆盖）。"""
    from .memory_store import _get_conn
    conn = _get_conn()
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    for m in messages[-max_turns * 2:]:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, datetime('now'))",
            (session_id, m.get("role", "human"), m.get("content", "")),
        )
    conn.commit()


def _summarize_and_persist(human: str, ai: str,
                           session_id: str) -> None:
    """将本轮重点摘要写入记忆（任务摘要 + 关于用户的洞察分层沉淀）。

    摘要约束（防丢关键决策参数）:
      1. 数字/日期/代码必须保留（金额、日期、基金代码等）
      2. 工具名 + 关键参数必须保留（恢复执行依赖）
      3. 实体名必须保留（人名/公司/项目名）

    洞察分层 (2026-08):
      - stable_insights: 稳定的用户偏好/习惯/关系 → 写 episodic (tags=[insight])
        （"用户偏好先给结论" 这类 3 年后仍有效的事实）
      - temporary_state: 临时状态（今天头痛、最近忙）→ 不持久化，
        避免把一次性状态误记为长期记忆
    """
    if len(human.strip()) < 8:
        return

    try:
        from .graphs.llm import get_chat_model
        from .memory_store import add_memory

        model = get_chat_model(temperature=0.1)
        prompt = (
            "Extract key facts, DECISIONS and PARAMETERS from this conversation turn.\n"
            "Output STRICT JSON: {\"summary\": \"任务摘要(中文,<120字)\", "
            "\"stable_insights\": [\"关于用户的稳定特质(偏好/习惯/关系,每条<60字)\"], "
            "\"temporary_state\": [\"临时状态(今天的心情/暂时的忙,不持久化)\"]}\n"
            "⚠️ summary MUST preserve ALL of:\n"
            "  - numbers / dates / codes (amounts, dates, fund codes, thresholds)\n"
            "  - tool names and their key arguments (e.g. vault_write → 简历_v2.md)\n"
            "  - entity names (people, companies, project names)\n"
            "If a decision changed a parameter (e.g. amount 1000→500), keep BOTH values.\n"
            "⚠️ stable_insights only for DURABLE traits (\"以后都\", \"习惯用\", preferences, "
            "relationship facts). Put one-off states (\"今天头痛\", \"最近在忙\") in temporary_state, "
            "never in stable_insights.\n"
            f"User: {human[:400]}\n"
            f"Assistant: {ai[:400]}"
        )
        resp = model.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        text = text.strip()
        if text.startswith("```"):
            import re
            text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("` \n")
        if text.startswith("{"):
            data = json.loads(text)
        else:
            # 降级：LLM 返回非 JSON 时把原文当任务摘要写入，不丢内容
            data = {"summary": text.strip('"').strip("'"), "stable_insights": [], "temporary_state": []}
    except Exception:
        # 降级：解析失败时把原文当任务摘要写入，不丢内容
        data = {"summary": text.strip().strip('"').strip("'"), "stable_insights": [], "temporary_state": []}

    summary = str(data.get("summary") or "").strip()
    if len(summary) >= 10:
        add_memory(summary, memory_type="conversation",
                   tags=["conversation", session_id[:16]], importance=4,
                   source="session_summary", session_id=session_id)

    # 稳定洞察 → episodic (可被 search_memories 跨会话召回)
    for insight in data.get("stable_insights", []) or []:
        insight = str(insight).strip().strip('"').strip("'")
        if len(insight) >= 6:
            add_memory(insight, memory_type="episodic",
                       tags=["insight", "auto"], importance=4,
                       source="session_summary", session_id=session_id)


def append_exchange(session_id: str,
                    human: str,
                    ai: str) -> list[dict[str, str]]:
    """追加一轮对话。每 SUMMARY_INTERVAL 轮附加一次 LLM 摘要。"""
    save_message(session_id, "human", human)
    save_message(session_id, "ai", ai)

    # 每 3 轮摘要一次
    all_msgs = get_session_messages(session_id, limit=100)
    human_count = sum(1 for m in all_msgs if m.get("role") == "human")
    if human_count % 3 == 0:
        _summarize_and_persist(human, ai, session_id)

    return all_msgs


def clear_session(session_id: str) -> None:
    """清空指定会话的历史消息 + 对应的 checkpoint 线程状态。

    checkpoint (SqliteSaver) 按 thread_id=chat_{session_id} 保存 chatbot 图的
    消息累积; 只清 messages 表不清 checkpoint 会导致"已清空的会话"仍带着
    旧历史恢复。
    """
    from .memory_store import _get_conn
    conn = _get_conn()
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.commit()
    try:
        from .checkpoint import delete_thread
        delete_thread(f"chat_{session_id}")
    except Exception:
        pass
