"""Context Pressure Monitor — 检测上下文膨胀并自动压缩。

问题: 会话历史全量注入会随时间膨胀（20 轮 × 长回答 ≈ 上万 token），
而 build_context 只有本轮注入预算，没有历史预算守卫。

方案:
  1. estimate_tokens() — 中英文混合 token 估算
  2. measure_pressure() — 计算历史 + 注入总压力，返回等级
  3. compress_history() — 超过阈值时压缩早期轮次为摘要行
  4. 接入 orchestrator 构造 messages 前 + build_context manifest
"""
from __future__ import annotations

import re
from typing import Any

# 压力阈值（token）
HISTORY_BUDGET = 4000        # 会话历史 token 预算
CONTEXT_BUDGET = 3500        # build_context 注入预算（与 build_context 默认一致）
KEEP_RECENT_TURNS = 6        # 压缩时保留的最近原文轮数
PRESSURE_YELLOW = 0.7        # 占用率 ≥70% → 黄色
PRESSURE_RED = 0.9           # 占用率 ≥90% → 红色

# 每轮摘要记忆的前缀（session_summary 写入时带）
_SUMMARY_TAG = "session_summary"


def estimate_tokens(text: str) -> int:
    """中英文混合 token 估算。

    中文 ≈ 0.7 token/字，英文 ≈ 0.25 token/字符。
    简化: 中文按 1 字 ≈ 1 token 保守，英文按 4 字符 ≈ 1 token。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[一-鿿]", text))
    other = len(text) - cjk
    return cjk + other // 4


def _messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


def measure_pressure(session_id: str | None = None,
                     history: list[dict[str, str]] | None = None,
                     context_tokens: int | None = None) -> dict[str, Any]:
    """测量上下文压力。

    Args:
        session_id: 会话 ID（用于加载历史，可选）
        history: 直接传入历史消息（优先于 session_id 加载）
        context_tokens: build_context 已用 token（可选）

    Returns:
        {
            "history_tokens": int,        # 历史消息 token
            "history_budget": int,
            "context_tokens": int,        # 本轮注入已用
            "context_budget": int,
            "total_tokens": int,
            "total_budget": int,
            "usage_ratio": float,         # 0~1+
            "level": "green" | "yellow" | "red",
            "compressed": bool,           # 是否已触发压缩
        }
    """
    msgs = history
    if msgs is None and session_id:
        try:
            from .memory_store import get_session_messages
            msgs = get_session_messages(session_id, limit=100)
        except Exception:
            msgs = []

    history_tokens = _messages_tokens(msgs or [])
    ctx_tokens = context_tokens if context_tokens is not None else 0

    total = history_tokens + ctx_tokens
    total_budget = HISTORY_BUDGET + CONTEXT_BUDGET
    ratio = total / total_budget if total_budget else 0

    if ratio >= PRESSURE_RED:
        level = "red"
    elif ratio >= PRESSURE_YELLOW:
        level = "yellow"
    else:
        level = "green"

    return {
        "history_tokens": history_tokens,
        "history_budget": HISTORY_BUDGET,
        "context_tokens": ctx_tokens,
        "context_budget": CONTEXT_BUDGET,
        "total_tokens": total,
        "total_budget": total_budget,
        "usage_ratio": round(ratio, 3),
        "level": level,
        "compressed": False,
    }


def _load_session_summaries(session_id: str, limit: int = 5) -> list[str]:
    """从 SQLite 加载该会话的摘要记忆（session_summary 写入的）。"""
    try:
        from .memory_store import _get_conn
        conn = _get_conn()
        rows = conn.execute(
            "SELECT content FROM memories WHERE memory_type='conversation' "
            "AND source='session_summary' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["content"] for r in rows if r["content"]]
    except Exception:
        return []


def compress_history(messages: list[dict[str, str]],
                     session_id: str | None = None,
                     budget: int = HISTORY_BUDGET) -> list[dict[str, str]]:
    """压缩历史消息：保留最近 K 轮原文 + 早期轮次压缩为摘要行。

    策略:
      1. 计算总 token；低于预算 → 原样返回
      2. 超过预算 → 保留最近 KEEP_RECENT_TURNS 轮原文
      3. 早期轮次尝试用 session_summary 摘要替代（从 SQLite 拉取）
      4. 摘要也不够 → 早期轮次合并为一条压缩行（每轮保留前 40 字）
    """
    if not messages:
        return messages

    total = _messages_tokens(messages)
    if total <= budget:
        return messages

    # 保留最近 K 轮原文
    keep = messages[-KEEP_RECENT_TURNS * 2:]  # 每轮含 human+ai 两条
    keep_tokens = _messages_tokens(keep)

    # 早期轮次
    early = messages[:-KEEP_RECENT_TURNS * 2]
    if not early:
        return keep

    # 用摘要替代早期（如果可用）
    summaries = _load_session_summaries(session_id) if session_id else []
    compressed: list[dict[str, str]] = []
    if summaries:
        compressed.append({
            "role": "ai",
            "content": f"[早期会话摘要]\n" + "\n".join(f"- {s[:120]}" for s in summaries[:4]),
        })
    else:
        # 无摘要 → 早期合并为压缩行（每轮保留关键内容）
        lines = []
        for m in early:
            role = "用户" if m.get("role") == "human" else "助手"
            text = m.get("content", "")[:60].replace("\n", " ")
            if text:
                lines.append(f"{role}: {text}")
        compressed.append({
            "role": "ai",
            "content": f"[早期会话压缩摘要]\n" + "\n".join(lines[-30:]),
        })

    result = compressed + keep

    # 最终仍超预算 → 再砍摘要行
    while _messages_tokens(result) > budget and len(result) > KEEP_RECENT_TURNS:
        # 砍掉摘要内容的一半
        if result[0]["content"].startswith("["):
            result[0]["content"] = result[0]["content"][:len(result[0]["content"]) // 2] + "\n...(截断)"
        if _messages_tokens(result) > budget and len(result) > 1:
            result = result[1:]

    return result


def format_pressure_line(p: dict[str, Any]) -> str:
    """格式化压力状态为一行（供日志/trace）。"""
    level = p.get("level", "green")
    icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(level, "🟢")
    return (f"{icon} context pressure={level} "
            f"history={p['history_tokens']}/{p['history_budget']} "
            f"ctx={p['context_tokens']}/{p['context_budget']} "
            f"total={p['total_tokens']}/{p['total_budget']} "
            f"({p['usage_ratio']:.0%})")
