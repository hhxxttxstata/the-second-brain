"""交互式 Chatbot CLI — 使用更新后的 agent 上下文架构。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date

from app.core.logging import logger


def handle_chat(text: str) -> str | None:
    from app.agent.session import get_default_session, load_messages, append_exchange
    from app.agent.approval_router import route_approval
    from app.agent.pending_ledger import get_pending_actions

    session_id = get_default_session()
    history = load_messages(session_id)

    # 检查待审批操作
    pending = get_pending_actions(session_id=session_id, status="pending_approval")
    if pending:
        approval = route_approval(text, session_id=session_id)
        if approval == "approved":
            _say("✅ 已批准，继续执行...")
        elif approval == "rejected":
            _say("❌ 已拒绝")

    from app.agent.graphs.orchestrator import run_orchestrator
    try:
        r = run_orchestrator(
            input_text=text,
            thread_id=session_id,
            conversation=history,
        )
        if r.get("success"):
            route = r.get("route", "?")
            result = r.get("result", "")

            # 写 session 日志
            if result:
                append_exchange(session_id, text, result)
            _write_summary(session_id, text, result, route, r.get("run_id", ""))

            return f"🤖 (→ {route})\n\n{result}"
        return f"❌ {r.get('error', '处理失败')}"
    except Exception as e:
        return f"❌ {e}"


def _say(msg: str) -> None:
    print(f"\n{msg}")


def _write_summary(session_id: str, question: str,
                   result: str, route: str, run_id: str) -> None:
    """写结构化 session summary 到 JSONL。"""
    try:
        from app.agent.handoff import get_active_handoffs
        from app.agent.trace import get_latest_trace
        from app.agent.session_jsonl import log_session_summary

        active_handoffs = get_active_handoffs()
        pending_handoffs = [
            {"task_id": h["task_id"], "action": h.get("pending_tool", ""), "status": h.get("status", "")}
            for h in active_handoffs
        ]

        tool_calls = []
        trace = get_latest_trace()
        if trace:
            tool_calls = trace.get("tool_calls", [])

        log_session_summary(
            session_id=session_id,
            goal=question[:120],
            completed=[f"路由={route}: 已回答"],
            pending=pending_handoffs or None,
            next_actions=[f"检查活跃 handoffs: {len(active_handoffs)} 个待处理"],
            evidence_refs=[f"trace://{run_id}"],
            summary=f"路由={route}: {question[:60]}",
            tool_count=len(tool_calls),
        )
    except Exception:
        pass


def main():
    from app.core.logging import configure_logging
    configure_logging()

    print()
    print("🤖 个人知识 Agent — 更新版上下文引擎")
    print("=" * 50)
    print("  基于 MEMORY.md 索引 + Task Handoff + Session Resume")
    print("  输入 help 查看命令 / quit 退出")
    print()

    while True:
        try:
            user_input = input("👤 > ")
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见")
            break

        text = user_input.strip()
        if not text:
            continue

        if text.lower() in ("quit", "exit", "q"):
            print("👋 再见")
            break

        if text.lower() in ("help", "h", "/?"):
            print("""
  plan         生成今日计划
  status       系统状态
  任意输入     直接对话（session+memory index+handoff 感知）
  help         显示帮助
  quit         退出
""")
            continue

        response = handle_chat(text)
        if response:
            print(f"\n🤖\n{response}\n")


if __name__ == "__main__":
    main()
