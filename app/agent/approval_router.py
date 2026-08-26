"""Approval Router — 审批路由中间件（轻量版）。

连接 pending_ledger + chatbot system prompt，实现跨会话审批延续。

流程 (2026-08 简化，去掉执行状态机):
  1. Agent 提议操作 → propose_action() → pending_ledger
  2. build_context() 从 pending_ledger 拉取待审批摘要 → 注入 system prompt
  3. 用户说"同意" → approve_action() 标记批准；用户说"拒绝" → reject_action()
  4. 执行由模型在对话流完成（prompt 规则：用户确认后立即执行），
     不再由 ledger 编排 executing/executed 状态（execution_log 已停用）
"""
from __future__ import annotations

from typing import Any

from .pending_ledger import (
    propose_action,
    approve_action,
    reject_action,
    get_pending_actions,
    get_pending_summary,
    get_pending_tasks_summary,
    init_ledger,
)

init_ledger()

# 当前会话正在等待审批的 key（用于跨会话延续）
_current_pending_keys: list[str] = []


def get_current_pending_keys() -> list[str]:
    return _current_pending_keys


def clear_pending_keys() -> None:
    _current_pending_keys.clear()


def route_approval(text: str, session_id: str, turn_id: str = "") -> str | None:
    """解析用户的审批意图。

    Returns:
      "approved" | "rejected" | None (not an approval response)
    """
    text_lower = text.strip().lower()

    # 检查是否在对 pending action 做回应
    pending = get_pending_actions(session_id=session_id, status="pending_approval")
    if not pending:
        return None

    # 同意/批准
    if any(kw in text_lower for kw in ("同意", "批准", "允许", "可以", "是", "yes", "好")):
        for action in pending:
            approve_action(action["idempotency_key"])
            if action["idempotency_key"] not in _current_pending_keys:
                _current_pending_keys.append(action["idempotency_key"])
        return "approved"

    # 拒绝
    if any(kw in text_lower for kw in ("拒绝", "不同意", "不准", "不行", "no", "不要")):
        for action in pending:
            reject_action(action["idempotency_key"])
        return "rejected"

    return None


def inject_pending_context() -> str:
    """构建 pending context 字符串（供 system prompt 注入）。"""
    parts = []
    pending = get_pending_actions(status="pending_approval")
    if pending:
        lines = ["\n## 待审批操作"]
        for a in pending:
            lines.append(f"  - [{a['action_type']}] {a['description']}")
            lines.append(f"    回复「同意」来批准执行")
        parts.append("\n".join(lines))

    approved = get_pending_actions(status="approved")
    if approved:
        lines = ["\n## 已批准待执行"]
        for a in approved:
            lines.append(f"  - [{a['action_type']}] {a['description']}")
        parts.append("\n".join(lines))

    return "\n".join(parts)
