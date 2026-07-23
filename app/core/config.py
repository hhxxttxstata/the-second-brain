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

    # Obsidian vault — 人类写的知识资产
    obsidian_vault: str = "D:/MYWORLD"

    # Agent data — 机器读写运行数据
    @property
    def agent_data_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "agent_data"

    # Feishu Bot
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_bot_name: str = "Agent助手"
    notify_webhook_url: str | None = None


settings = Settings()
