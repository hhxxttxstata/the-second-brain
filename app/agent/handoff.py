"""Task Handoff — 结构化任务传递 Artifact。

不依赖对话摘要来"记住"未完成的工作。
每个需要跨会话完成的任务都有独立的 Markdown Handoff 文件。

结构:
  tasks/
  ├── active_tasks.jsonl        ← 活跃任务清单
  ├── pending_approvals.jsonl   ← 待审批任务清单
  └── completed_tasks.jsonl     ← 已完成任务归档

  handoffs/
  ├── task_001.md               ← 具体任务传递（含 Goal / Completed / Pending / Next Step）
  └── task_002.md

工作流:
  1. Agent 识别要跨会话的任务 → create_handoff()
  2. 写入 handoffs/task_NNN.md + tasks/active_tasks.jsonl
  3. 如果需审批 → 写入 tasks/pending_approvals.jsonl
  4. 新会话启动 → build_context 加载 active + pending handoffs
  5. 完成 → complete_handoff() → 移到 completed_tasks.jsonl

依赖关系：任务可以变长，但不依赖聊天历史长短。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

_TASKS_DIR = settings.agent_data_dir / "tasks"
_HANDOFFS_DIR = settings.agent_data_dir / "handoffs"


def _ensure_dirs() -> None:
    _TASKS_DIR.mkdir(parents=True, exist_ok=True)
    _HANDOFFS_DIR.mkdir(parents=True, exist_ok=True)


def _next_id() -> str:
    """生成自增 task ID。"""
    _ensure_dirs()
    existing = list(_HANDOFFS_DIR.glob("task_*.md"))
    max_num = 0
    for f in existing:
        try:
            num = int(f.stem.replace("task_", ""))
            if num > max_num:
                max_num = num
        except ValueError:
            pass
    return f"task_{max_num + 1:03d}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 写 Handoff
# ---------------------------------------------------------------------------


def create_handoff(
    goal: str,
    pending_tool: str = "",
    pending_params: dict[str, Any] | None = None,
    completed: list[str] | None = None,
    next_step: str = "",
    forbidden: list[str] | None = None,
    requires_approval: bool = False,
) -> dict[str, Any]:
    """创建一个 Task Handoff。

    Args:
        goal: 任务目标（一句话）
        pending_tool: 待执行的工具名
        pending_params: 待执行的工具参数
        completed: 已完成步骤列表
        next_step: 下一步描述
        forbidden: 禁止行为列表
        requires_approval: 是否需要审批

    Returns:
        {"task_id": "task_001", "handoff_path": "...", "approval_key": "..."}
    """
    _ensure_dirs()
    task_id = _next_id()
    now = datetime.now().isoformat()

    params = pending_params or {}
    content_str = json.dumps(params, ensure_ascii=False, sort_keys=True)
    content_hash = _content_hash(content_str)

    # 写入 handoff Markdown
    lines = [
        f"# Task Handoff",
        f"",
        f"Task ID: {task_id}",
        f"Status: {'awaiting_approval' if requires_approval else 'in_progress'}",
        f"Created: {now}",
        f"",
        f"## Goal",
        f"{goal}",
        f"",
    ]

    if completed:
        lines.extend(["## Completed", ""])
        for c in completed:
            lines.append(f"- [x] {c}")
        lines.append("")

    if pending_tool:
        lines.extend([
            "## Pending action",
            f"Tool: {pending_tool}",
            f"Mode: write",
            f"Params hash: {content_hash}",
            "",
            f"Content:",
            f"```json",
            json.dumps(params, ensure_ascii=False, indent=2),
            f"```",
            "",
        ])

    if next_step:
        lines.extend(["## Next step", f"{next_step}", ""])

    if forbidden:
        lines.extend(["## Forbidden", ""])
        for f_val in forbidden:
            lines.append(f"- {f_val}")
        lines.append("")

    handoff_path = _HANDOFFS_DIR / f"{task_id}.md"
    handoff_path.write_text("\n".join(lines), encoding="utf-8")

    # 写入 active JSONL
    record = {
        "task_id": task_id,
        "status": "pending_approval" if requires_approval else "active",
        "goal": goal[:200],
        "pending_tool": pending_tool,
        "content_hash": content_hash,
        "completed_count": len(completed or []),
        "created_at": now,
        "approval_key": "",
    }
    _append_jsonl("active_tasks", record)

    approval_key = ""
    if requires_approval:
        approval_key = f"approval_{task_id}"
        record["approval_key"] = approval_key
        _append_jsonl("pending_approvals", {
            "approval_key": approval_key,
            "task_id": task_id,
            "goal": goal[:200],
            "pending_tool": pending_tool,
            "params_hash": content_hash,
            "created_at": now,
        })

    return {
        "task_id": task_id,
        "handoff_path": str(handoff_path),
        "approval_key": approval_key,
    }


def complete_handoff(task_id: str, result: str = "") -> bool:
    """标记一个 handoff 为已完成。移到 completed_tasks.jsonl。"""
    _ensure_dirs()

    # 从 active_tasks 读出并移除
    active = _read_jsonl("active_tasks")
    entry = None
    remaining = []
    for a in active:
        if a.get("task_id") == task_id:
            entry = a
            a["status"] = "completed"
            a["completed_at"] = datetime.now().isoformat()
            a["result"] = result[:200]
            _append_jsonl("completed_tasks", a)
        else:
            remaining.append(a)
    if entry is None:
        return False

    # 写回 active（删掉该条）
    _write_jsonl("active_tasks", remaining)

    # 如果 pending_approvals 中有，也标记完成
    pending = _read_jsonl("pending_approvals")
    pending = [p for p in pending if p.get("task_id") != task_id]
    _write_jsonl("pending_approvals", pending)

    # 可选更新 handoff markdown 状态
    handoff_path = _HANDOFFS_DIR / f"{task_id}.md"
    if handoff_path.exists():
        content = handoff_path.read_text(encoding="utf-8")
        content = content.replace("Status: awaiting_approval", "Status: completed")
        content = content.replace("Status: in_progress", "Status: completed")
        handoff_path.write_text(content, encoding="utf-8")

    return True


def update_handoff_status(task_id: str, new_status: str,
                          new_next_step: str = "") -> bool:
    """更新 handoff 状态（不完成）。"""
    _ensure_dirs()

    # 更新 active_tasks
    active = _read_jsonl("active_tasks")
    found = False
    for a in active:
        if a.get("task_id") == task_id:
            a["status"] = new_status
            found = True
            break
    if found:
        _write_jsonl("active_tasks", active)

    # 更新 markdown
    handoff_path = _HANDOFFS_DIR / f"{task_id}.md"
    if handoff_path.exists():
        content = handoff_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        new_lines = []
        in_next = False
        for line in lines:
            if line.startswith("Status: "):
                new_lines.append(f"Status: {new_status}")
                continue
            if line.startswith("## Next step"):
                in_next = True
            if in_next and new_next_step and line.strip().startswith("- ["):
                continue  # skip old next steps
            new_lines.append(line)
        if new_next_step:
            new_lines.append(new_next_step)
        handoff_path.write_text("\n".join(new_lines), encoding="utf-8")

    return found


# ---------------------------------------------------------------------------
# 读 Handoff
# ---------------------------------------------------------------------------


def get_active_handoffs() -> list[dict[str, Any]]:
    """获取当前活跃的 handoffs（含 pending approval）。"""
    active = _read_jsonl("active_tasks")
    pending = _read_jsonl("pending_approvals")
    pending_ids = {p.get("task_id") for p in pending}
    combined = []

    for p in pending:
        pid = p.get("task_id", "")
        handoff_path = _HANDOFFS_DIR / f"{pid}.md"
        handoff_content = handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else ""
        combined.append({
            "task_id": pid,
            "status": "pending_approval",
            "goal": p.get("goal", ""),
            "approval_key": p.get("approval_key", ""),
            "pending_tool": p.get("pending_tool", ""),
            "handoff_content": handoff_content[:500],
        })

    for a in active:
        aid = a.get("task_id", "")
        if aid in pending_ids:
            continue  # 已包含在 pending 中
        handoff_path = _HANDOFFS_DIR / f"{aid}.md"
        handoff_content = handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else ""
        combined.append({
            "task_id": aid,
            "status": a.get("status", "active"),
            "goal": a.get("goal", ""),
            "approval_key": "",
            "pending_tool": a.get("pending_tool", ""),
            "handoff_content": handoff_content[:500],
        })

    return combined


def get_handoff_by_id(task_id: str) -> dict[str, Any] | None:
    """按 ID 获取 handoff。"""
    handoff_path = _HANDOFFS_DIR / f"{task_id}.md"
    if not handoff_path.exists():
        return None

    content = handoff_path.read_text(encoding="utf-8")
    # 从 active/pending 中查状态
    status = "unknown"
    for lst_name in ("active_tasks", "pending_approvals", "completed_tasks"):
        items = _read_jsonl(lst_name)
        for item in items:
            if item.get("task_id") == task_id:
                status = item.get("status", status)
                break
        if status != "unknown":
            break

    return {
        "task_id": task_id,
        "status": status,
        "content": content,
    }


def get_handoff_context() -> str:
    """构建 handoff 摘要字符串（供 context 注入）。"""
    handoffs = get_active_handoffs()
    if not handoffs:
        return ""

    lines = ["\n## 活跃任务 (Handoff)"]
    for h in handoffs:
        status_icon = {"pending_approval": "⏳", "active": "🔄", "completed": "✅", "in_progress": "🔄"}.get(
            h["status"], "•")
        lines.append(f"  {status_icon} {h['task_id']}: {h['goal'][:100]}")
        if h["status"] == "pending_approval":
            lines.append(f"    待审批: {h['pending_tool']} (key: {h['approval_key'][:20]}...)")
            lines.append(f"    回复「同意」来批准")
    return "\n".join(lines)


def get_pending_approval_context() -> str:
    """获取待审批的 handoff 摘要。"""
    pending = _read_jsonl("pending_approvals")
    if not pending:
        return ""

    lines = ["\n## 待审批操作"]
    for p in pending:
        handoff_path = _HANDOFFS_DIR / f"{p.get('task_id', '')}.md"
        content = handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else ""
        goal = ""
        for line in content.split("\n"):
            if line.startswith("## Goal"):
                continue
            if line.startswith("## "):
                break
            if line.strip() and not line.startswith("#"):
                goal = line.strip()[:120]
                break

        lines.append(f"  ⏳ [{p.get('task_id', '')}] {goal or p.get('goal', '')}")
        lines.append(f"    工具: {p.get('pending_tool', '')}")
        lines.append(f"    回复「同意」执行此操作")

    return "\n".join(lines)


def approve_handoff(approval_key: str) -> bool:
    """批准一个 handoff。"""
    pending = _read_jsonl("pending_approvals")
    found = None
    remaining = []
    for p in pending:
        if p.get("approval_key") == approval_key:
            found = p
            p["status"] = "approved"
            p["approved_at"] = datetime.now().isoformat()
        else:
            remaining.append(p)

    if found is None:
        return False

    # 更新 active_tasks 状态
    active = _read_jsonl("active_tasks")
    for a in active:
        if a.get("task_id") == found.get("task_id"):
            a["status"] = "approved"
            break
    _write_jsonl("active_tasks", active)

    # 从 pending 移除
    _write_jsonl("pending_approvals", remaining)

    # 更新 handoff markdown
    task_id = found.get("task_id", "")
    handoff_path = _HANDOFFS_DIR / f"{task_id}.md"
    if handoff_path.exists():
        content = handoff_path.read_text(encoding="utf-8")
        content = content.replace("Status: awaiting_approval", "Status: approved")
        handoff_path.write_text(content, encoding="utf-8")

    return True


# ---------------------------------------------------------------------------
# 内部：JSONL 读写
# ---------------------------------------------------------------------------


def _jsonl_path(name: str) -> Path:
    return _TASKS_DIR / f"{name}.jsonl"


def _read_jsonl(name: str) -> list[dict[str, Any]]:
    path = _jsonl_path(name)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        return [json.loads(l) for l in lines if l.strip()]
    except (json.JSONDecodeError, OSError):
        return []


def _write_jsonl(name: str, items: list[dict[str, Any]]) -> None:
    path = _jsonl_path(name)
    path.write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(name: str, item: dict[str, Any]) -> None:
    path = _jsonl_path(name)
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
