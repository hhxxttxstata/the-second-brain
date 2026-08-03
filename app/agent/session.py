"""会话管理 — 持久化多轮对话消息 + 自动摘要写入情景记忆。

每轮对话的消息保存到 SQLite messages 表，
下次调用时从 SQLite 加载并注入到 MessagesState。

同时，每 3 轮批量摘要一次，写入 episodic 记忆作为跨会话的长期保留。
"""
from __future__ import annotations

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
    """将本轮重点摘要写入 episodic 记忆。"""
    if len(human.strip()) < 8:
        return

    try:
        from .graphs.llm import get_chat_model
        from .memory_store import add_memory

        model = get_chat_model(temperature=0.1)
        prompt = (
            "Extract key facts and intent from this conversation turn. "
            "Output a concise single sentence in Chinese (under 100 chars):\n"
            f"User: {human[:300]}\n"
            f"Assistant: {ai[:300]}"
        )
        resp = model.invoke(prompt)
        summary = resp.content if hasattr(resp, "content") else str(resp)
        summary = summary.strip().strip('"').strip("'")
        if len(summary) < 10:
            return

        add_memory(summary, memory_type="conversation",
                   tags=["conversation", session_id[:16]], importance=4,
                   source="session_summary", session_id=session_id)
    except Exception:
        pass


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
    """清空指定会话的历史消息。"""
    from .memory_store import _get_conn
    conn = _get_conn()
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.commit()
