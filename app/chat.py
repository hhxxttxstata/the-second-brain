"""交互式 Chatbot CLI — 纯 Obsidian 版，直接调用 agent graphs。"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date

_THREAD_ID = None


def _thread() -> str:
    global _THREAD_ID
    if _THREAD_ID is None:
        _THREAD_ID = f"chat_{uuid.uuid4().hex[:10]}"
    return _THREAD_ID


def handle_chat(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None

    if text in ("plan", "计划"):
        from app.agent.graphs.plan_graph import run_plan_graph
        r = run_plan_graph()
        if r.get("success"):
            items = r.get("items", [])
            lines = [f"📋 今日计划 ({r.get('date', date.today().isoformat())})\n"]
            for i, item in enumerate(items, 1):
                icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item.get("priority", "medium"), "•")
                badge = {"diary_todo": "📓", "pending_task": "🔄", "habit": "🏃",
                         "goal": "🎯", "new": "💡", "default": ""}.get(item.get("source", ""), "")
                lines.append(f"  {i}. {icon} {badge} [{item.get('priority', 'MEDIUM')}] {item.get('title', '')}")
            return "\n".join(lines)
        return f"❌ {r.get('error', '生成失败')}"

    if text in ("status", "状态"):
        from app.agent.self_eval import run_self_eval, print_report
        report = run_self_eval()
        return print_report(report)

    if text in ("help", "h", "/?"):
        return (
            "🤖 个人知识 Agent — Obsidian 原生版\n\n"
            "  plan         生成今日计划\n"
            "  status       系统状态\n"
            "  任意输入     直接对话（有上下文记忆）\n"
            "  help         显示帮助\n"
            "  quit         退出"
        )

    if text in ("quit", "exit", "q"):
        return "__EXIT__"

    # 默认：Orchestrator 自动路由，保持同一 thread 保证对话连续性
    from app.agent.graphs.orchestrator import run_orchestrator
    try:
        r = run_orchestrator(input_text=text, thread_id=_thread())
        if r.get("success"):
            route = r.get("route", "?")
            result = r.get("result", "")
            return f"🤖 (→ {route})\n\n{result}"
        return f"❌ {r.get('error', '处理失败')}"
    except Exception as e:
        return f"❌ {e}"


def main():
    print()
    print("🤖 个人知识 Agent — 纯 Obsidian 架构")
    print("=" * 50)
    print("  你的所有笔记/日记就是我的数据库")
    print("  输入 help 查看命令 / quit 退出")
    print()

    while True:
        try:
            user_input = input("👤 > ")
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见")
            break

        response = handle_chat(user_input)
        if response == "__EXIT__":
            print("👋 再见")
            break
        if response:
            print(f"\n🤖\n{response}\n")


if __name__ == "__main__":
    main()
