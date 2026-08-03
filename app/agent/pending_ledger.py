"""Pending Action Ledger — 待审批 + 会话间任务连续性。

一个操作的生命周期:
  proposed → pending_approval → approved → executed
                              → rejected

idempotency_key 确保同一操作不会执行两次。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .memory_store import _get_conn


def init_ledger() -> None:
    """建表。幂等。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT    NOT NULL,
            turn_id         TEXT    NOT NULL DEFAULT '',
            action_type     TEXT    NOT NULL,  -- vault_write | vault_append | task_op | memory_write
            description     TEXT    NOT NULL,  -- 人类可读的说明
            params_json     TEXT    NOT NULL DEFAULT '{}',  -- 执行参数
            idempotency_key TEXT    NOT NULL UNIQUE,  -- 幂等键
            status          TEXT    NOT NULL DEFAULT 'pending_approval',
            -- pending_approval | approved | executing | executed | rejected | failed
            result_summary  TEXT    DEFAULT '',
            error           TEXT    DEFAULT '',
            cancelled_by    TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_pending_session
            ON pending_actions(session_id, status);
        CREATE INDEX IF NOT EXISTS idx_pending_status
            ON pending_actions(status);
        CREATE INDEX IF NOT EXISTS idx_pending_idempotent
            ON pending_actions(idempotency_key);

        CREATE TABLE IF NOT EXISTS execution_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT    NOT NULL,
            action_type     TEXT    NOT NULL,
            session_id      TEXT    NOT NULL,
            status          TEXT    NOT NULL,  -- started | succeeded | failed
            detail          TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_exec_idempotent
            ON execution_log(idempotency_key);
    """)
    conn.commit()


# ── 写 ──


def propose_action(
    session_id: str,
    action_type: str,
    description: str,
    params: dict[str, Any] | None = None,
    turn_id: str = "",
) -> dict[str, Any]:
    """提一条需要审批的操作；自动生成 idempotency_key。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    key = f"{action_type}_{uuid.uuid4().hex[:12]}"

    conn.execute(
        """INSERT INTO pending_actions
           (session_id, turn_id, action_type, description, params_json,
            idempotency_key, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)""",
        (session_id, turn_id, action_type, description,
         json.dumps(params or {}, ensure_ascii=False),
         key, now, now),
    )
    conn.commit()

    return {
        "idempotency_key": key,
        "action_type": action_type,
        "description": description,
        "status": "pending_approval",
    }


def approve_action(key: str) -> bool:
    """批准一条操作。返回 True 表示批准成功。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    cursor = conn.execute(
        "UPDATE pending_actions SET status='approved', updated_at=? WHERE idempotency_key=? AND status='pending_approval'",
        (now, key),
    )
    conn.commit()
    return cursor.rowcount > 0


def reject_action(key: str) -> bool:
    """拒绝一条操作。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    cursor = conn.execute(
        "UPDATE pending_actions SET status='rejected', updated_at=? WHERE idempotency_key=? AND status='pending_approval'",
        (now, key),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_executing(key: str) -> bool:
    """标记为执行中。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    cursor = conn.execute(
        "UPDATE pending_actions SET status='executing', updated_at=? WHERE idempotency_key=? AND status='approved'",
        (now, key),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO execution_log (idempotency_key, action_type, session_id, status, created_at) "
        "SELECT idempotency_key, action_type, session_id, 'started', ? FROM pending_actions WHERE idempotency_key=?",
        (now, key),
    )
    conn.commit()
    return True


def mark_executed(key: str, result: str = "", error: str = "") -> None:
    """标记为已执行。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    if error:
        conn.execute(
            "UPDATE pending_actions SET status='failed', result_summary=?, error=?, updated_at=? WHERE idempotency_key=?",
            (result[:200], error[:200], now, key),
        )
    else:
        conn.execute(
            "UPDATE pending_actions SET status='executed', result_summary=?, updated_at=? WHERE idempotency_key=?",
            (result[:200], now, key),
        )
    conn.execute(
        "INSERT INTO execution_log (idempotency_key, action_type, session_id, status, detail, created_at) "
        "SELECT idempotency_key, action_type, session_id, ?, ?, ? FROM pending_actions WHERE idempotency_key=?",
        ("succeeded" if not error else "failed", (result or error)[:200], now, key),
    )
    conn.commit()


# ── 查 ──


def get_pending_actions(session_id: str | None = None,
                        status: str = "pending_approval") -> list[dict[str, Any]]:
    """获取待处理的操作。"""
    conn = _get_conn()
    if session_id:
        rows = conn.execute(
            "SELECT * FROM pending_actions WHERE session_id=? AND status=? ORDER BY created_at",
            (session_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pending_actions WHERE status=? ORDER BY created_at DESC LIMIT 20",
            (status,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_action_by_key(key: str) -> dict[str, Any] | None:
    """按 idempotency_key 获取操作。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM pending_actions WHERE idempotency_key=?", (key,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def has_been_executed(key: str) -> bool:
    """检查是否已执行过（幂等检查）。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM pending_actions WHERE idempotency_key=? AND status IN ('executed', 'executing')",
        (key,),
    ).fetchone()
    return row is not None


def get_pending_summary() -> str:
    """返回待审批操作的文本摘要（供 system prompt 注入）。"""
    actions = get_pending_actions(status="pending_approval")
    if not actions:
        return ""

    lines = ["\n## 待审批操作"]
    for a in actions:
        lines.append(f"- [{a['action_type']}] {a['description']} (key: {a['idempotency_key'][:16]}...)")
        lines.append(f"  回复「同意」来批准，回复「拒绝」来取消")
    return "\n".join(lines)


def get_pending_tasks_summary(session_id: str) -> str:
    """返回当前会话的待审批/进行中任务摘要。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM pending_actions
           WHERE session_id=? AND status IN ('pending_approval', 'approved', 'failed')
           ORDER BY created_at DESC LIMIT 5""",
        (session_id,),
    ).fetchall()
    if not rows:
        return ""

    lines = ["\n## 上个会话未完成的操作"]
    for r in rows:
        a = _row_to_dict(r)
        icon = {"pending_approval": "⏳", "approved": "✅待执行", "failed": "❌"}.get(
            a["status"], "•")
        lines.append(f"  {icon} [{a['action_type']}] {a['description']}")
        if a["status"] == "pending_approval":
            lines.append(f"    回复「同意」执行此操作")
        elif a["status"] == "failed":
            lines.append(f"    错误: {a['error']}")
    return "\n".join(lines)


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    if "params_json" in d and isinstance(d["params_json"], str):
        try:
            d["params"] = json.loads(d["params_json"])
        except (json.JSONDecodeError, TypeError):
            d["params"] = {}
    return d
