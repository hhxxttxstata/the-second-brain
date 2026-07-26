"""LLM factory — returns a configured ChatOpenAI pointing at DeepSeek API."""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from app.core.config import settings


def get_chat_model(**kwargs: Any) -> ChatOpenAI:
    """Create a ChatOpenAI instance configured for DeepSeek."""
    # DeepSeek V4 Flash 默认关闭思考模式，避免长延迟
    extra_body = {}
    if "flash" in (settings.llm_model or "").lower():
        extra_body["thinking"] = {"type": "disabled"}

    return ChatOpenAI(
        model=kwargs.pop("model", settings.llm_model or "deepseek-chat"),
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url or "https://api.deepseek.com",
        temperature=kwargs.pop("temperature", 0.7),
        extra_body=extra_body,
        **kwargs,
    )
