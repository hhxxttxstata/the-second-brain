"""Structured LLM output — 消灭"手搓 regex 剥壳 + json.loads"类失败。

统一封装两条路径：
  - 真 LangChain 模型（ChatOpenAI/DeepSeek）→ with_structured_output(json_mode)，
    模型层保证输出合法 JSON，失败类型从"解析失败"收敛为"字段校验失败"；
  - 无 with_structured_output 能力的模型（测试 FakeModel、自定义 stub）→
    invoke + parse_llm_json + Pydantic 校验，行为等价（测试 mock 零改动）。

parse_llm_json 是最后的稳健解析兜底（剥代码块、截取 {} 区间），
供无法使用 schema 的场合（如解析子进程 stdout 中的 JSON 行）。
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.logging import logger

T = TypeVar("T", bound=BaseModel)

# 流式 writer 跨子图桥：langgraph 子图 invoke 会重建 context，外层 stream writer
# 在子图节点内取不到。orchestrator 在调用子图前捕获 writer 存入线程本地，
# chatbot 等子图节点同线程读取（stream_orchestrator 专用，非流式路径恒为 None）。
STREAM_WRITER_BRIDGE = threading.local()


def parse_llm_json(text: str) -> Any:
    """从 LLM 文本输出中稳健提取 JSON 对象。

    容错顺序：直接解析 → 剥 markdown 代码块 → 截取首尾大括号区间
    （模型在 JSON 前后附带说明文字时仍可解析）。
    失败抛 ValueError（不再是 json.JSONDecodeError，调用方统一 except Exception）。
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = cleaned.rstrip("` \n\t")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("no valid JSON object in LLM output")


def invoke_structured(
    model: Any,
    schema: type[T],
    prompt: str,
    retries: int = 1,
    validator: Any = None,
) -> tuple[T, Any]:
    """按 Pydantic schema 获取结构化输出。

    Args:
        validator: 可选回调 (parsed) -> bool；False 视为本次失败并触发重试
            （用于 schema 之外的语义校验，如"标签全非法 → 重试"）。

    Returns:
        (parsed, raw): parsed 为 schema 实例；raw 为原始 LLM 响应
        （供 usage_metadata 等 token 统计，FakeModel 路径可能为 None）。

    Raises:
        ValueError: 重试耗尽仍无法产出符合 schema 的结果。
    """
    attempts = max(1, retries + 1)
    last_error = ""

    if hasattr(model, "with_structured_output"):
        chain = model.with_structured_output(schema, method="json_mode", include_raw=True)
        for attempt in range(attempts):
            try:
                out = chain.invoke(prompt)
                if out.get("parsing_error"):
                    raise ValueError(str(out["parsing_error"])[:300])
                parsed = out.get("parsed")
                if parsed is None:
                    raise ValueError("structured output returned no parsed object")
                if validator is not None and not validator(parsed):
                    raise ValueError("output rejected by validator")
                return parsed, out.get("raw")
            except Exception as exc:
                last_error = str(exc) or type(exc).__name__
                logger.warning("structured.retry",
                               attempt=attempt + 1, schema=schema.__name__, error=last_error[:200])
        raise ValueError(f"structured output failed after {attempts} attempts: {last_error}")

    # 兼容路径：测试 FakeModel / 无 with_structured_output 的模型
    for attempt in range(attempts):
        raw = model.invoke(prompt)
        text = raw.content if hasattr(raw, "content") else str(raw)
        try:
            data = parse_llm_json(text)
            parsed = schema.model_validate(data)
            if validator is not None and not validator(parsed):
                raise ValueError("output rejected by validator")
            return parsed, raw
        except Exception as exc:
            last_error = str(exc) or type(exc).__name__
            logger.warning("structured.retry_compat",
                           attempt=attempt + 1, schema=schema.__name__, error=last_error[:200])
    raise ValueError(f"structured output failed after {attempts} attempts: {last_error}")
