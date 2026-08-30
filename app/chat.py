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
    from app.agent.session import get_default_session, append_exchange
    from app.agent.approval_router import route_approval
    from app.agent.pending_ledger import get_pending_actions

    session_id = get_default_session()

    # 检查待审批操作
    pending = get_pending_actions(session_id=session_id, status="pending_approval")
    if pending:
        approval = route_approval(text, session_id=session_id)
        if approval == "approved":
            _say("✅ 已批准，继续执行...")
        elif approval == "approved_partial":
            remaining = get_pending_actions(session_id=session_id, status="pending_approval")
            _say(f"✅ 已批准被点名的操作，剩余 {len(remaining)} 条仍待审批")
        elif approval == "rejected":
            _say("❌ 已拒绝")

    from app.agent.graphs.orchestrator import run_orchestrator
    try:
        # 2026-08 改造: 不再手工 load_messages 注入历史 —— 历史由 chatbot 图
        # 的 SqliteSaver checkpoint 按固定 thread_id=session_id 自动累积。
        r = run_orchestrator(
            input_text=text,
            thread_id=session_id,
        )
        if r.get("success"):
            route = r.get("route", "?")
            result = r.get("result", "")

            # 写 session 日志
            if result:
                append_exchange(session_id, text, result)
            _write_summary(session_id, text, result, route,
                           r.get("trace_id", "") or r.get("run_id", ""))

            return f"🤖 (→ {route})\n\n{result}"
        return f"❌ {r.get('error', '处理失败')}"
    except Exception as e:
        return f"❌ {e}"


def _say(msg: str) -> None:
    print(f"\n{msg}")


def _write_summary(session_id: str, question: str,
                   result: str, route: str, trace_id: str) -> None:
    """写语义化 session summary 到 JSONL（工具轨迹 + LLM 提炼 + handoff 关联）。"""
    try:
        from app.agent.trace import get_latest_trace
        from app.agent.session_jsonl import log_session_summary_semantic

        log_session_summary_semantic(
            session_id=session_id,
            question=question,
            answer=result,
            route=route,
            trace=get_latest_trace(),
            trace_id=trace_id,
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
  model        切换 LLM 模型（model 2 / model reasoner / model list）
  help         显示帮助
  quit         退出
  任意输入     直接对话（session+memory index+handoff 感知）
""")
            continue

        # 模型切换命令
        if text.lower() in ("model", "model list", "model ls", "模型"):
            from app.agent.model_switch import format_model_menu
            print(f"\n{format_model_menu()}\n")
            continue
        if text.lower().startswith("model "):
            from app.agent.model_switch import (
                format_model_menu, resolve_model_ref, set_current_model,
                get_current_model_config,
            )
            ref = text[6:].strip()
            mid = resolve_model_ref(ref)
            if mid:
                mc = set_current_model(mid)
                print(f"\n✅ 已切换到: {mc['label']} ({mc['model']})\n")
                # 清空工具缓存（工具绑定不依赖模型，无需重建）
                print(f"  后续对话将使用 {mc['model']}\n")
            else:
                print(f"\n⚠️ 无法识别模型: {ref}")
                print(f"{format_model_menu()}\n")
            continue

        response = handle_chat(text)
        if response:
            print(f"\n🤖\n{response}\n")


if __name__ == "__main__":
    main()
