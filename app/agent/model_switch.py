"""模型切换 — 运行时切换 LLM 模型，不修改 .env。

可用模型列表 + 当前选择持久化到 agent_data/model_config.json，
get_chat_model() 读取当前选择；chat.py 通过 /model 命令切换。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings

_CONFIG_PATH = settings.agent_data_dir / "model_config.json"

# 可用模型预设（可扩展：同 API 多模型 / 不同 API base_url）
AVAILABLE_MODELS: list[dict[str, Any]] = [
    {
        "id": "deepseek-v4-flash",
        "label": "DeepSeek V4 Flash（默认，快）",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "LLM_API_KEY",
        "temperature": 0.7,
    },
    {
        "id": "deepseek-chat",
        "label": "DeepSeek Chat（通用）",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "LLM_API_KEY",
        "temperature": 0.7,
    },
    {
        "id": "deepseek-reasoner",
        "label": "DeepSeek Reasoner（推理强，慢）",
        "model": "deepseek-reasoner",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "LLM_API_KEY",
        "temperature": 0.3,
    },
    {
        "id": "gpt-4o-mini",
        "label": "GPT-4o-mini（OpenAI）",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "temperature": 0.7,
    },
    {
        "id": "claude-sonnet-4-5",
        "label": "Claude Sonnet 4.5（Anthropic）",
        "model": "claude-sonnet-4-5",
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "temperature": 0.7,
    },
]

MODEL_ALIASES = {
    "flash": "deepseek-v4-flash",
    "chat": "deepseek-chat",
    "reasoner": "deepseek-reasoner",
    "gpt": "gpt-4o-mini",
    "claude": "claude-sonnet-4-5",
}


def _load_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_current_model_id() -> str:
    """当前生效的模型 ID（配置 > .env 的 LLM_MODEL > 默认）。"""
    cfg = _load_config()
    saved = cfg.get("model_id", "")
    if saved:
        return saved
    env_model = settings.llm_model or "deepseek-chat"
    for m in AVAILABLE_MODELS:
        if m["model"] == env_model:
            return m["id"]
    return "deepseek-v4-flash"


def get_current_model_config() -> dict[str, Any]:
    """当前生效的完整模型配置。"""
    mid = get_current_model_id()
    for m in AVAILABLE_MODELS:
        if m["id"] == mid:
            return m
    return AVAILABLE_MODELS[0]


def set_current_model(model_id: str) -> dict[str, Any]:
    """切换当前模型。返回切换后的配置。"""
    cfg = _load_config()
    cfg["model_id"] = model_id
    _save_config(cfg)
    return get_current_model_config()


def list_models() -> list[dict[str, Any]]:
    """列出所有可用模型 + 当前标记。"""
    current = get_current_model_id()
    return [m | {"current": m["id"] == current} for m in AVAILABLE_MODELS]


def resolve_model_ref(ref: str) -> str | None:
    """把用户输入（id / alias / 数字序号）解析成 model_id。"""
    ref = ref.strip().lower()
    if ref in MODEL_ALIASES:
        return MODEL_ALIASES[ref]
    for m in AVAILABLE_MODELS:
        if m["id"].lower() == ref:
            return m["id"]
        if ref.isdigit() and int(ref) - 1 == AVAILABLE_MODELS.index(m):
            return m["id"]
    return None


def format_model_menu() -> str:
    """格式化模型列表（供 /model 命令显示）。"""
    lines = ["📦 可用模型："]
    for i, m in enumerate(list_models(), 1):
        marker = "✅" if m["current"] else "  "
        lines.append(f"  {marker} {i}. {m['label']}  ({m['id']})")
    lines.append("")
    lines.append("用法: model <序号|id|别名>  (如: model 2 / model reasoner / model gpt)")
    lines.append("      model / model list  查看列表")
    return "\n".join(lines)
