"""Shared LangGraph checkpointer — SqliteSaver backed by agent_data/checkpoint.db.

为什么需要它:
  LangGraph 的 checkpointer 是"图的执行状态快照": 同一个 thread_id 的多次
  graph.invoke 会从上次结束的状态继续, MessagesState 的 messages 自动累积,
  无需手工 load_messages 再注入历史。

实现说明:
  - 同步 SqliteSaver 内部自带 threading.Lock (见 langgraph.checkpoint.sqlite
    源码), 配合 check_same_thread=False 的连接可在多线程 (FastAPI 线程池 /
    Streamlit) 下安全使用 —— 这是官方文档推荐模式。
  - 进程内全局单例; checkpoint.db 与业务记忆 memory.db 分离, 互不干扰
    (checkpointer 使用固定的 checkpoints 系列表名)。
"""
from __future__ import annotations

import sqlite3
import threading

from app.core.config import settings

from langgraph.checkpoint.sqlite import SqliteSaver

_checkpoint_path = settings.agent_data_dir / "checkpoint.db"

_saver: SqliteSaver | None = None
_saver_lock = threading.Lock()


def get_checkpointer() -> SqliteSaver:
    """返回进程共享的 SqliteSaver 实例 (懒加载, 线程安全)。"""
    global _saver
    if _saver is None:
        with _saver_lock:
            if _saver is None:
                _checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(_checkpoint_path), check_same_thread=False)
                _saver = SqliteSaver(conn)
    return _saver


def delete_thread(thread_id: str) -> None:
    """删除一个 thread 的全部检查点 (用于 clear_session 等场景)。"""
    if not thread_id:
        return
    try:
        get_checkpointer().delete_thread(thread_id)
    except Exception:
        # 删除失败不应阻断业务 (checkpoint 只是恢复用的状态快照)
        pass
