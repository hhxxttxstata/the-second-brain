"""Agent Terminal UI — 产品级终端交互界面（Rich）

启动:
    python -m app.webui

特性:
    - 左右分栏：左侧对话 / 右侧实时 Agent 思考过程
    - 工具调用链、上下文来源、路由决策可视化
    - 命令面板：/status /memory /help /clear /traces
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from threading import Lock
from typing import Any

from rich.align import Align
from rich.box import MINIMAL, ROUNDED
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.traceback import install as install_rich_traceback

from app.agent.graphs.orchestrator import run_orchestrator
from app.agent.agent_data_service import (
    add_episodic,
    format_episodic,
    format_tasks,
)
from app.agent.self_eval import run_self_eval
from app.agent.trace import load_all_traces, get_trace_stats
from app.feedback import get_feedback_stats, load_all_feedback, new_feedback, save_feedback
from app.core.logging import configure_logging
from app.core.config import settings

install_rich_traceback()
configure_logging()

console = Console()

ROUTE_LABELS = {
    "chatbot": "💬 对话回答",
    "plan": "📋 每日计划",
    "reflect": "🔍 反思分析",
    "memory": "🧠 记忆管理",
    "error": "❌ 出错",
}


# ===================================================================
# State
# ===================================================================

class AppState:
    def __init__(self) -> None:
        self.lock = Lock()
        self.messages: list[dict] = []
        self.status_message = "就绪"
        self.thinking_process: list | None = None

    def add_message(self, role: str, content: str, **meta: Any) -> None:
        with self.lock:
            self.messages.append({"role": role, "content": content, **meta})
            if len(self.messages) > 100:
                self.messages = self.messages[-100:]

    def set_thinking(self, blocks: list | None) -> None:
        with self.lock:
            self.thinking_process = blocks

    def set_status(self, msg: str) -> None:
        with self.lock:
            self.status_message = msg

    def snapshot(self) -> tuple[list[dict], list | None, str]:
        with self.lock:
            return list(self.messages), self.thinking_process, self.status_message


STATE = AppState()


# ===================================================================
# Helpers
# ===================================================================

def load_trace(trace_id: str) -> dict | None:
    path = settings.agent_data_dir / "traces" / f"{trace_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def build_thought_blocks(route: str, reason: str, latency: int,
                         trace: dict | None) -> list:
    """构建思考过程的 Rich 元素列表。"""
    blocks: list = []
    label = ROUTE_LABELS.get(route, route)

    # 路由决策
    blocks.append(Text("\n🧭 路由决策", style="bold yellow"))
    blocks.append(Text(f"  {label}  ·  ⚡{latency}ms"))
    if reason:
        blocks.append(Text(f"  └ {reason}", style="grey53"))

    if trace:
        # 上下文来源
        sources = trace.get("context_sources", [])
        if sources:
            blocks.append(Text("\n📚 上下文来源", style="bold yellow"))
            for s in sources:
                blocks.append(
                    Text(f"  · {s.get('layer', '?')}/{s.get('type', '?')}"
                         f" ({s.get('chars', 0)} chars)", style="grey53"))

        # 工具调用链
        calls = trace.get("tool_calls", [])
        if calls:
            blocks.append(Text("\n🔧 工具调用链", style="bold yellow"))
            for tc in calls:
                ok = tc.get("success", False)
                name = tc.get("name", "?")
                tlat = tc.get("latency_ms", 0)
                params = tc.get("params", {})
                preview = tc.get("result_preview", "")[:80]
                icon = "✅" if ok else "❌"
                blocks.append(Text(f"  {icon} {name}  ({tlat}ms)",
                                   style="bold" if ok else "red"))
                if params:
                    blocks.append(Text(f"    参数: {json.dumps(params, ensure_ascii=False)[:100]}",
                                       style="grey53"))
                if preview:
                    blocks.append(Text(f"    结果: {preview}", style="grey53"))

        # 记忆更新
        updates = trace.get("memory_updates", [])
        if updates:
            blocks.append(Text("\n🧠 记忆更新", style="bold yellow"))
            for m in updates:
                blocks.append(Text(f"  · [{m.get('type', '?')}] {m.get('preview', '')[:60]}",
                                   style="grey53"))

    blocks.append(Text(f"\n⏱ 总耗时 {latency}ms", style="grey42"))
    return blocks


# ===================================================================
# Renderers
# ===================================================================

def render_chat() -> Panel:
    msgs, _, status = STATE.snapshot()
    if not msgs:
        return Panel(
            Align.center(Text("\n\n✨ 开始对话吧\n输入 /help 查看命令", style="grey42")),
            title="💬 对话",
            border_style="blue",
            box=ROUNDED,
        )

    elements: list = []
    for m in msgs:
        role = m["role"]
        content = m.get("content", "")
        route = m.get("route", "")

        if role == "user":
            elements.append(Text("\n👤 你", style="bold cyan"))
            elements.append(Panel(Text(content, overflow="fold"),
                                  box=MINIMAL, style="cyan"))
        else:
            route_str = ""
            if route and route in ROUTE_LABELS:
                route_str = f"  ({ROUTE_LABELS[route]})"
            lat = m.get("latency", 0)
            lat_str = f"  ⚡{lat}ms" if lat else ""
            elements.append(Text(f"\n🤖 Agent{route_str}{lat_str}", style="bold green"))
            if any(c in content for c in "#*`-"):
                elements.append(Panel(Markdown(content), box=MINIMAL, style="green"))
            else:
                elements.append(Panel(Text(content, overflow="fold"),
                                      box=MINIMAL, style="green"))

    return Panel(Group(*elements), title="💬 对话", border_style="blue", box=ROUNDED)


def render_thinking() -> Panel:
    _, blocks, status = STATE.snapshot()
    if not blocks:
        return Panel(
            Align.center(Text(f"\n{status}", style="grey42")),
            title="🧠 Agent 思考过程",
            border_style="dim",
            box=ROUNDED,
        )
    return Panel(Group(*blocks), title="🧠 Agent 思考过程",
                 border_style="yellow", box=ROUNDED)


def render_status() -> Panel:
    traces = load_all_traces(limit=50)
    stats = get_trace_stats(traces)
    eval_report = {}
    try:
        eval_report = run_self_eval()
    except Exception:
        pass

    lines: list = [
        Text("\n🤖 Agent 指标", style="bold blue"),
    ]
    if stats.get("total", 0) > 0:
        lines.append(Text(f"  运行次数: {stats['total']}"))
        lines.append(Text(f"  成功率:   {stats['success_rate']}%"))
        lines.append(Text(f"  平均延迟: {stats['avg_latency_ms']}ms"))
        lines.append(Text(f"  工具调用: {stats['total_tool_calls']}"))
        lines.append(Text(f"  记忆更新: {stats['total_memory_updates']}"))
        by_type = stats.get("by_type", {})
        if by_type:
            lines.append(Text("\n📂 任务类型分布", style="bold blue"))
            for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
                icon = ROUTE_LABELS.get(t, "•")
                lines.append(Text(f"  {icon}: {'█' * min(c, 20)} {c}", style="grey53"))
    else:
        lines.append(Text("  暂无运行记录", style="grey42"))

    lines.append(Text("\n🧠 记忆层", style="bold blue"))
    lines.append(Text(f"  情景记忆: {len(format_episodic(limit=5))} chars"))
    lines.append(Text(f"  任务记忆: {len(format_tasks())} chars"))

    if eval_report:
        score = eval_report.get("overall_score", 0)
        lines.append(Text("\n📊 健康评分", style="bold blue"))
        lines.append(Text(f"  {score}/100",
                          style="bold green" if score >= 60 else "bold red"))

    lines.append(Text("\n⚙️ 配置", style="bold blue"))
    lines.append(Text(f"  LLM:   {settings.llm_model}"))
    lines.append(Text(f"  Vault: {settings.obsidian_vault}"))

    return Panel(Group(*lines), title="📊 系统状态",
                 border_style="green", box=ROUNDED)


def render_traces() -> Panel:
    traces = load_all_traces(limit=15)
    if not traces:
        return Panel(
            Align.center(Text("暂无运行记录", style="grey42")),
            title="📋 运行记录", border_style="dim", box=ROUNDED,
        )

    t = Table(box=MINIMAL, show_header=True, header_style="bold")
    t.add_column("时间", style="grey53", width=8)
    t.add_column("类型", width=10)
    t.add_column("摘要", width=38)
    t.add_column("状态", width=4)
    t.add_column("耗时", width=8)
    t.add_column("工具", width=4)

    for tr in traces:
        t.add_row(
            tr.get("timestamp", "")[11:19],
            tr.get("task_type", "?")[:8],
            tr.get("user_intent", "?")[:22],
            "🟢" if tr.get("success") else "🔴",
            f'{tr.get("latency_ms", 0)}ms',
            str(len(tr.get("tool_calls", []))),
        )
    return Panel(t, title="📋 最近运行记录",
                 border_style="cyan", box=ROUNDED)


def render_memory() -> Panel:
    text = format_episodic(limit=20)
    return Panel(
        Text(text if text else "暂无情景记忆",
             style="grey53", overflow="fold"),
        title="🧠 情景记忆", border_style="green", box=ROUNDED,
    )


def render_feedback() -> Panel:
    """反馈统计面板。"""
    stats = get_feedback_stats()
    if stats.get("total", 0) == 0:
        return Panel(
            Align.center(Text("暂无反馈记录", style="grey42")),
            title="📝 反馈统计", border_style="dim", box=ROUNDED,
        )

    lines = [
        Text(f"\n📝 反馈统计", style="bold blue"),
        Text(f"  总反馈: {stats['total']}"),
        Text(f"  👍 有用: {stats['useful']} ({stats['useful_rate']}%)"),
        Text(f"  👎 没用: {stats['useless']}"),
    ]

    by_type = stats.get("by_failure_type", {})
    type_labels = {"tool_wrong": "⚠️ 工具调用错", "memory_wrong": "🧠 记忆写错",
                   "obsidian_wrong": "📚 Obsidian检索错", "permission_wrong": "🔒 权限策略错"}
    if by_type:
        lines.append(Text(f"\n📂 失败类型分布", style="bold blue"))
        for t, c in by_type.items():
            label = type_labels.get(t, t)
            lines.append(Text(f"  {label}: {c}"))

    recent = stats.get("recent", [])
    if recent:
        lines.append(Text(f"\n⏱ 最近反馈", style="bold blue"))
        for fb in recent:
            label = type_labels.get(fb["type"], fb["type"])
            lines.append(Text(f"  [{fb['time']}] {label} {fb.get('input', '')[:30]}",
                              style="grey53"))

    all_fb = load_all_feedback()
    for fb in all_fb[:5]:
        note = fb.get("feedback_note", "")
        if note:
            lines.append(Text(f"  ✏️ {note[:80]}", style="grey42"))

    return Panel(
        Group(*lines),
        title="📝 反馈", border_style="magenta", box=ROUNDED,
    )


def render_help() -> Panel:
    return Panel(
        Text("""\
可用命令:
  /help     显示帮助
  /status   系统状态 & 指标
  /memory   查看情景记忆
  /traces   最近运行记录
  /feedback 反馈统计
  /fb [1-6] 快速评价上次回复
           1=👍有用  2=👎没用  3=⚠️工具调用错
           4=🧠记忆写错  5=📚Obsidian检索错  6=🔒权限策略错
  /clear    清屏
  /quit     退出

普通输入直接与 Agent 对话。\
""", style="grey53"),
        title="📖 帮助", border_style="yellow", box=ROUNDED,
    )


# ===================================================================
# Commands
# ===================================================================

def handle_command(cmd: str, _last_trace: list = None) -> Panel | None:
    """返回覆盖右侧的面板，None=恢复默认。"""
    if _last_trace is None:
        _last_trace = []

    c = cmd.strip().lower()
    if c == "/help":
        return render_help()
    if c == "/status":
        return render_status()
    if c == "/traces":
        return render_traces()
    if c == "/memory":
        return render_memory()
    if c == "/feedback":
        return render_feedback()
    if c == "/clear":
        with STATE.lock:
            STATE.messages.clear()
            STATE.thinking_process = None
        return None
    if c in ("/quit", "/exit", "/q"):
        console.print("\n👋 再见", style="bold yellow")
        sys.exit(0)

    # /fb <1-6>  — 对上一次回复打分
    if c.startswith("/fb "):
        if not _last_trace:
            return Panel(Text("还没有可评价的回复", style="grey53"),
                         title="⚠️ 反馈", border_style="yellow", box=ROUNDED)
        choice = c[4:].strip()
        type_map = {
            "1": "useful", "2": "useless",
            "3": "tool_wrong", "4": "memory_wrong",
            "5": "obsidian_wrong", "6": "permission_wrong",
        }
        ft = type_map.get(choice)
        if not ft:
            return Panel(Text("用法: /fb <数字>\n1=👍有用  2=👎没用  3=⚠️工具调用错\n4=🧠记忆写错  5=📚Obsidian检索错  6=🔒权限策略错",
                              style="grey53"),
                         title="⚠️ 反馈", border_style="yellow", box=ROUNDED)
        trace_id, input_text, trace_data = _last_trace
        fb = new_feedback(trace_id, ft, input_text, trace_data)
        save_feedback(fb)
        return Panel(Text(f"✅ 已记录: {ft}", style="bold green"),
                     title="📝 反馈", border_style="green", box=ROUNDED)

    return None


# ===================================================================
# Layout
# ===================================================================

HEADER = Text.assemble(
    ("🤖 Agentic Data Platform", "bold white"),
    ("  ·  Obsidian-native  ·  DeepSeek  ·  LangGraph", "grey42"),
)


def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="body", ratio=1),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )
    return layout


def redraw(layout: Layout, right_override: Panel | None = None) -> None:
    layout["header"].update(Panel(HEADER, box=MINIMAL, style="bold"))
    layout["left"].update(render_chat())
    layout["right"].update(right_override or render_thinking())


# ===================================================================
# Main
# ===================================================================

def main() -> None:
    console.clear()
    console.print(HEADER)
    console.print()

    layout = make_layout()
    right_panel: Panel | None = None
    last_trace: list = []  # [trace_id, input_text, trace_data]

    def _run_agent(text: str) -> tuple[str | None, dict | None]:
        """运行 Agent，返回 (trace_id, trace_data)。"""
        nonlocal right_panel, last_trace
        right_panel = None
        STATE.set_thinking(None)
        STATE.set_status("Agent 思考中...")

        chat_id = f"tui_{uuid.uuid4().hex[:8]}"
        start = time.monotonic()
        result = run_orchestrator(input_text=text, thread_id=chat_id)
        latency = int((time.monotonic() - start) * 1000)

        reply = result.get("result", "") if result.get("success") else \
            f"❌ {result.get('error', '未知错误')}"
        route = result.get("route", "?")
        reason = result.get("route_reason", "")
        trace_id = result.get("run_id", "")
        trace_data = load_trace(trace_id) if trace_id else None

        blocks = build_thought_blocks(route, reason, latency, trace_data)
        STATE.set_thinking(blocks)
        STATE.add_message("assistant", reply, route=route, reason=reason,
                          latency=latency, trace_id=trace_id)
        STATE.set_status("就绪")

        # 保存为"最后一次 trace"供 /fb 使用
        if trace_id:
            last_trace[:] = [trace_id, text, trace_data]
        return trace_id, trace_data

    # ── 外层循环：每问一次完整退出 Live，下轮重建 ──
    type_map = {
        "1": "useful", "2": "useless",
        "3": "tool_wrong", "4": "memory_wrong",
        "5": "obsidian_wrong", "6": "permission_wrong",
    }

    while True:
        with Live(layout, console=console, screen=True, refresh_per_second=8) as live:
            redraw(layout)
            live.refresh()

            try:
                user_input = console.input("[bold cyan]>[/bold cyan] ")
            except (EOFError, KeyboardInterrupt):
                console.print("\n👋 再见", style="bold yellow")
                return

            text = user_input.strip()
            if not text:
                continue

            STATE.add_message("user", text)

            if text.startswith("/"):
                result = handle_command(text, last_trace)
                right_panel = result
                redraw(layout, right_panel)
                live.refresh()
                continue

            redraw(layout)
            live.refresh()

            trace_id, trace_data = _run_agent(text)
            redraw(layout)
            live.refresh()

        # ── Live 退出 → 终端正常模式 → 反馈交互 → 下轮重建 Live ──
        if trace_id:
            print()
            print("─" * 44)
            print("📝 给这条回复打分？")
            print("  [0] 跳过    [1] 👍 有用    [2] 👎 没用")
            print("  [3] ⚠️ 工具调用错  [4] 🧠 记忆写错")
            print("  [5] 📚 Obsidian检索错  [6] 🔒 权限策略错")
            print("─" * 44)
            try:
                choice = input("选择 [0]: ").strip() or "0"
            except (EOFError, KeyboardInterrupt):
                choice = "0"
            ft = type_map.get(choice)
            if ft and choice != "0":
                fb = new_feedback(trace_id, ft, text, trace_data)
                if ft != "useful":
                    try:
                        note = input("  ✏️ 补充说明（可选，回车跳过）: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        note = ""
                    if note:
                        fb["feedback_note"] = note
                save_feedback(fb)
                print(f"  ✅ 已记录")
            input("  按回车继续...")


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.upper() in ("GBK", "GB2312", "CP936"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
