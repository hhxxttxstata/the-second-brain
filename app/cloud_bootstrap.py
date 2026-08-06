"""云端启动引导 — 无 vault 模式初始化 + 健康检查。

Render/Railway 部署时作为启动命令入口:
    python app/cloud_bootstrap.py

职责:
    1. 初始化 agent_data 目录结构（traces/feedback/tasks/handoffs/memory...）
    2. 无 vault 模式下创建空 vault 目录（避免路径不存在报错）
    3. 输出部署健康信息
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app.core.config import settings


def init_agent_data() -> None:
    """初始化 agent_data 目录结构。"""
    data_dir = settings.agent_data_dir
    subdirs = [
        "traces", "benchmark", "feedback", "tasks", "handoffs",
        "memory", "outputs", "code_runs", "session_logs", "eval",
    ]
    for s in subdirs:
        (data_dir / s).mkdir(parents=True, exist_ok=True)

    # 空 memory 文件（build_context 需要）
    policy = data_dir / "memory" / "system_policy.md"
    if not policy.exists():
        policy.write_text(
            "# 系统策略\n\n"
            "- Agent 以对话/记忆/任务/工具能力为主（云部署无本地 vault）\n"
            "- 谨慎对待删除操作\n"
            "- 回答基于实际检索/工具结果，不编造\n",
            encoding="utf-8",
        )

    index = data_dir / "memory" / "MEMORY.md"
    if not index.exists():
        index.write_text(
            "# 记忆索引\n\n_云部署初始状态，随对话积累。_\n",
            encoding="utf-8",
        )

    print(f"✅ agent_data 已初始化: {data_dir}")


def init_vault() -> None:
    """无 vault 模式：确保 vault 目录存在（可为空）。"""
    vault = Path(settings.obsidian_vault)
    if not vault.exists():
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "notes").mkdir(exist_ok=True)
        (vault / "diaries").mkdir(exist_ok=True)
        (vault / "README.md").write_text(
            "# Vault（云部署）\n\n此目录在云端，初始为空。笔记同步后续接入。\n",
            encoding="utf-8",
        )
        print(f"✅ vault 已初始化（空）: {vault}")
    else:
        print(f"ℹ️ vault 已存在: {vault}")


def main() -> None:
    print("🚀 云部署引导启动...")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  agent_data: {settings.agent_data_dir}")
    print(f"  vault: {settings.obsidian_vault}")
    print(f"  model: {settings.llm_model}")

    init_agent_data()
    init_vault()

    # 验证核心模块可导入
    try:
        from app.agent.graphs.tools import get_agent_tools
        tools = get_agent_tools()
        print(f"✅ 工具加载: {len(tools)} 个")
    except Exception as exc:
        print(f"⚠️ 工具加载失败: {exc}")

    print("✅ 引导完成")


if __name__ == "__main__":
    main()
