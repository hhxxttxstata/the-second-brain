"""Feedback — 每次 Agent 运行后的用户反馈系统。

每次 trace 结束后提示用户评分，JSON 保存到 agent_data/feedback/。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_FEEDBACK_DIR = settings.agent_data_dir / "feedback"

FAILURE_TYPES = {
    "useful": "👍 有用",
    "useless": "👎 没用",
    "tool_wrong": "⚠️ 工具调用错",
    "memory_wrong": "🧠 记忆写错",
    "obsidian_wrong": "📚 Obsidian 检索错",
    "permission_wrong": "🔒 权限策略错",
}

# ═══════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════

FEEDBACK_SCHEMA = {
    "$schema": "https://json-schema.org/draft-2020-12/schema",
    "type": "object",
    "required": ["trace_id", "failure_type", "input"],
    "properties": {
        "trace_id": {"type": "string"},
        "input": {"type": "string"},
        "failure_type": {"type": "string", "enum": list(FAILURE_TYPES.keys())},
        "expected_behavior": {"type": "string"},
        "actual_behavior": {"type": "string"},
        "sanitized_context_sources": {"type": "array"},
        "feedback_note": {"type": "string"},
    },
}


def new_feedback(trace_id: str, failure_type: str, input_text: str = "",
                 trace_data: dict | None = None) -> dict[str, Any]:
    """构造一条反馈记录。"""
    fb: dict[str, Any] = {
        "feedback_id": f"fb_{uuid.uuid4().hex[:10]}",
        "trace_id": trace_id,
        "failure_type": failure_type,
        "input": input_text[:200],
        "timestamp": datetime.now().isoformat(),
        "expected_behavior": "",
        "actual_behavior": "",
        "sanitized_context_sources": [],
        "feedback_note": "",
    }

    # 从 trace 中提取脱敏的上下文来源（只保留 layer/type，去掉 content）
    if trace_data:
        sources = trace_data.get("context_sources", [])
        fb["sanitized_context_sources"] = [
            {"layer": s.get("layer", "?"), "type": s.get("type", "?"), "chars": s.get("chars", 0)}
            for s in sources
        ]
        fb["expected_behavior"] = f"Agent 应正确响应: {trace_data.get('user_intent', '')[:100]}"
        fb["actual_behavior"] = trace_data.get("final_output", "")[:200]

    return fb


# ═══════════════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════════════

def _ensure_dir() -> Path:
    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return _FEEDBACK_DIR


def save_feedback(fb: dict[str, Any]) -> str:
    """保存一条反馈到 agent_data/feedback/。"""
    path = _ensure_dir() / f"{fb['feedback_id']}.json"
    path.write_text(json.dumps(fb, ensure_ascii=False, indent=2), encoding="utf-8")
    return fb["feedback_id"]


def load_all_feedback(limit: int = 200) -> list[dict[str, Any]]:
    """加载所有反馈记录，按时间倒序。"""
    if not _FEEDBACK_DIR.exists():
        return []
    files = sorted(_FEEDBACK_DIR.glob("fb_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files[:limit]:
        try:
            result.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return result


def get_feedback_stats() -> dict[str, Any]:
    """反馈统计。"""
    all_fb = load_all_feedback()
    if not all_fb:
        return {"total": 0}

    by_type: dict[str, int] = {}
    for fb in all_fb:
        t = fb.get("failure_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    useful = by_type.pop("useful", 0)
    useless = by_type.pop("useless", 0)
    total_feedback = len(all_fb)

    return {
        "total": total_feedback,
        "useful": useful,
        "useless": useless,
        "useful_rate": round(useful / total_feedback * 100, 1) if total_feedback else 0,
        "by_failure_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "recent": [{
            "time": fb.get("timestamp", "")[11:19],
            "type": fb.get("failure_type", "?"),
            "input": fb.get("input", "")[:40],
        } for fb in all_fb[:10]],
    }


# ═══════════════════════════════════════════════════════════════════
# TUI 交互（已废弃 — webui.py 直接内联，不移除以保兼容）
# ═══════════════════════════════════════════════════════════════════

FEEDBACK_PROMPT = """──────────────────────────────────────────────
📝 给这条回复打分？
  [0] 跳过
  [1] 👍 有用    [2] 👎 没用
  [3] ⚠️ 工具调用错    [4] 🧠 记忆写错
  [5] 📚 Obsidian检索错    [6] 🔒 权限策略错
──────────────────────────────────────────────"""


def prompt_feedback_in_tui(
    trace_id: str,
    input_text: str,
    trace_data: dict | None,
) -> str | None:
    """终端内嵌反馈提示，返回 feedback_id 或 None。"""
    from rich.console import Console
    from rich.prompt import Prompt

    console = Console()
    console.print(FEEDBACK_PROMPT, style="grey53")

    choice = Prompt.ask("选择", default="0", console=console)
    choice = choice.strip()

    type_map = {
        "1": "useful",
        "2": "useless",
        "3": "tool_wrong",
        "4": "memory_wrong",
        "5": "obsidian_wrong",
        "6": "permission_wrong",
    }

    failure_type = type_map.get(choice)
    if not failure_type or choice == "0":
        return None

    fb = new_feedback(trace_id, failure_type, input_text, trace_data)

    # 如果是负面反馈，追加输入框让用户描述
    if failure_type != "useful":
        note = Prompt.ask(
            "  ✏️ 补充说明（可选，直接回车跳过）",
            default="",
            console=console,
        )
        if note.strip():
            fb["feedback_note"] = note.strip()

    save_feedback(fb)
    console.print(f"  ✅ 已记录 ({FAILURE_TYPES[failure_type]})", style="bold green")
    return fb["feedback_id"]
