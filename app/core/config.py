from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "dev", "test", "prod"] = "local"
    debug: bool = True

    llm_provider: str = "deepseek"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com/v1"
    # planner 专用小模型（cascade：小模型判意图，大模型答内容）
    # 置空则跟随 llm_model；deepseek-v4-flash 在 llm.py 中自动禁 thinking，延迟低
    planner_model: str = "deepseek-v4-flash"
    # 规则短路层开关：纯问候/显式记忆指令等高置信意图跳过 planner LLM
    planner_shortcut: bool = True

    # Obsidian vault — 人类写的知识资产
    # 云部署时通过 OBSIDIAN_VAULT 环境变量指定（可为空目录，无 vault 模式）
    obsidian_vault: str = "D:/MYWORLD"

    # Agent data — 机器读写运行数据
    # 云部署时通过 AGENT_DATA_DIR 环境变量覆盖（默认项目根/agent_data）
    # pydantic-settings 自动映射 AGENT_DATA_DIR → 本字段（Path 类型自动转换）
    agent_data_dir: Path = Path(__file__).resolve().parent.parent.parent / "agent_data"



settings = Settings()
