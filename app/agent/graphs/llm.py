"""LLM factory — returns a configured ChatOpenAI pointing at DeepSeek API."""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from app.core.config import settings


def get_chat_model(**kwargs: Any) -> ChatOpenAI:
    """Create a ChatOpenAI instance configured for the current model.

    模型来源（优先级）:
      1. kwargs["model"] — 调用方显式指定（最高优先级）
      2. 运行时切换（model_config.json，通过 /model 命令设置）
      3. .env 的 LLM_MODEL
    """
    # 运行时切换的模型配置
    try:
        from ..model_switch import get_current_model_config
        mc = get_current_model_config()
        runtime_model = mc.get("model", "")
        runtime_base_url = mc.get("base_url", "")
        runtime_temperature = mc.get("temperature", 0.7)
        runtime_key_env = mc.get("api_key_env", "LLM_API_KEY")
    except Exception:
        runtime_model, runtime_base_url = "", ""
        runtime_temperature, runtime_key_env = 0.7, "LLM_API_KEY"

    import os
    # 运行时 API key（按模型预设的 env 名）
    runtime_api_key = os.environ.get(runtime_key_env) or settings.llm_api_key

    # DeepSeek V4 Flash 默认关闭思考模式，避免长延迟
    # 显式传 model=None 与不传等价（调用方以 None 表示"跟随全局/运行时配置"）
    model_name = kwargs.get("model") or runtime_model or settings.llm_model or "deepseek-chat"
    extra_body = {}
    if "flash" in (model_name or "").lower():
        extra_body["thinking"] = {"type": "disabled"}

    return ChatOpenAI(
        model=kwargs.pop("model", None) or model_name,
        api_key=kwargs.pop("api_key", runtime_api_key),
        base_url=kwargs.pop("base_url", runtime_base_url or settings.llm_base_url or "https://api.deepseek.com"),
        temperature=kwargs.pop("temperature", runtime_temperature),
        extra_body=extra_body,
        **kwargs,
    )
