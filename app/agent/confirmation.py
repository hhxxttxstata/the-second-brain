"""Human-in-the-loop 审批系统。

当 Agent 试图执行需要人工确认的操作（如修改 Obsidian）时，
暂停执行，通过 CLI 弹确认框，用户批准后才继续。
"""
from __future__ import annotations

from typing import Any

_PENDING_CONFIRMATION: dict[str, Any] | None = None


def need_confirmation(tool_name: str, params: dict[str, Any],
                      description: str) -> bool:
    """标记当前工具调用需要人工确认。

    返回 False 表示"等待确认中，不要继续执行"。
    """
    global _PENDING_CONFIRMATION
    _PENDING_CONFIRMATION = {
        "tool_name": tool_name,
        "params": params,
        "description": description,
    }
    return False


def get_pending() -> dict[str, Any] | None:
    """获取当前待审批的操作。"""
    return _PENDING_CONFIRMATION


def confirm_pending() -> bool:
    """返回 True=已批准, False=拒绝。"""
    global _PENDING_CONFIRMATION
    pending = _PENDING_CONFIRMATION
    if not pending:
        return False

    print()
    print("─" * 44)
    print("🔐 需要你的批准")
    print(f"  工具: {pending['tool_name']}")
    print(f"  参数: {_fmt_params(pending['params'])}")
    print(f"  说明: {pending['description']}")
    print("─" * 44)

    while True:
        try:
            choice = input("  批准执行？[y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "n"
        if choice in ("y", "yes"):
            _PENDING_CONFIRMATION = None
            return True
        elif choice in ("", "n", "no"):
            _PENDING_CONFIRMATION = None
            print("  ❌ 已拒绝")
            return False


def _fmt_params(params: dict[str, Any]) -> str:
    """格式化参数为可读字符串。"""
    parts = []
    for k, v in params.items():
        s = str(v)[:80]
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def clear_pending() -> None:
    global _PENDING_CONFIRMATION
    _PENDING_CONFIRMATION = None
