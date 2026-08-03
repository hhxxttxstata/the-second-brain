"""用户性格分析与风格适配 — 从日记/对话中提取性格特质，动态调整 LLM 回答风格。

架构：
  1. 性格分析 —— 读取用户的日记/记忆/对话, 提取性格标签
  2. 风格配置 —— 根据性格标签生成 System Prompt 风格指令
  # © 2026 tata. All rights reserved. """ 3. 持久化 —— 将分析结果写入 SQLite memory 表的 profile 字段
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from app.core.config import settings
from app.core.logging import logger

# ── 性格维度与风格映射 ──

PERSONA_DIMENSIONS = {
    "formality": {
        "description": "正式程度",
        "tags": ["formal", "casual", "mixed"],
    },
    "detail_level": {
        "description": "详细程度",
        "tags": ["detailed", "concise", "balanced"],
    },
    "emotional_expression": {
        "description": "情感表达",
        "tags": ["expressive", "reserved", "analytical"],
    },
    "topic_focus": {
        "description": "主题偏好",
        "tags": ["technical", "reflective", "planning", "mixed"],
    },
    "decision_style": {
        "description": "决策风格",
        "tags": ["decisive", "exploratory", "cautious"],
    },
}

STYLE_INSTRUCTIONS: dict[str, str] = {
    "formal": "使用正式、礼貌的语气，避免缩略语和网络用语。",
    "casual": "用朋友般的语气，可以适当使用口语化和表情符号。",
    "detailed": "回答尽可能全面、深入，提供多个视角和详细推理过程。",
    "concise": "回答精简，直接给出结论，不啰嗦过程。",
    "expressive": "适当表达情感共鸣，用温暖、支持性的语言。",
    "reserved": "保持专业距离，专注于事实和逻辑分析。",
    "analytical": "以数据、逻辑和结构化分析为主。",
    "technical": "优先使用技术术语、架构图、代码示例来交流。",
    "reflective": "多提问、引导用户自我思考。",
    "planning": "优先输出结构化的计划、步骤、优先级。",
    "decisive": "直接给出建议，减少模棱两可的表达。",
    "cautious": "列出多个方案并分析利弊，不做强硬推荐。",
}


def analyze_persona(diaries: str, memories: list[str],
                     conversations: list[str] | None = None) -> dict[str, Any]:
    """从用户日记和对话中分析性格特质。

    Returns:
        {dimension: tag, ...}
    """
    text_pool = []
    if diaries:
        text_pool.append(diaries)
    text_pool.extend(memories[:10])
    if conversations:
        text_pool.extend(conversations[-20:])

    combined = "\n".join(text_pool)[:5000]
    if not combined.strip():
        return {"formality": "casual", "detail_level": "balanced",
                "emotional_expression": "expressive", "topic_focus": "mixed",
                "decision_style": "exploratory"}

    persona = {}

    # 用启发式规则分析
    # 正式 vs 随意
    formality_score = _count_formal_markers(combined)
    persona["formality"] = "formal" if formality_score > 3 else "casual"

    # 详细程度
    avg_line_len = sum(len(l) for l in combined.split("\n") if l.strip()) / max(
        len([l for l in combined.split("\n") if l.strip()]), 1)
    persona["detail_level"] = "detailed" if avg_line_len > 60 else (
        "concise" if avg_line_len < 20 else "balanced")

    # 情感表达
    emotional_words = ["感觉", "觉得", "好累", "开心", "焦虑", "担心",
                       "喜欢", "讨厌", "希望", "怕", "怀疑", "迷茫"]
    emotional_count = sum(combined.count(w) for w in emotional_words)
    persona["emotional_expression"] = "expressive" if emotional_count > 5 else (
        "analytical" if emotional_count < 2 else "reserved")

    # 主题偏好
    tech_keywords = ["架构", "代码", "开发", "部署", "算法", "系统", "配置"]
    reflect_keywords = ["反思", "思考", "感悟", "为什么", "意义", "选择"]
    plan_keywords = ["计划", "安排", "步骤", "明天", "下周", "todo"]
    tech_s = sum(combined.count(w) for w in tech_keywords)
    reflect_s = sum(combined.count(w) for w in reflect_keywords)
    plan_s = sum(combined.count(w) for w in plan_keywords)
    max_theme = max(tech_s, reflect_s, plan_s)
    if max_theme == 0:
        persona["topic_focus"] = "mixed"
    elif tech_s == max_theme:
        persona["topic_focus"] = "technical"
    elif reflect_s == max_theme:
        persona["topic_focus"] = "reflective"
    elif plan_s == max_theme:
        persona["topic_focus"] = "planning"
    else:
        persona["topic_focus"] = "mixed"

    # 决策风格
    decisive_words = ["决定", "必须", "一定", "确认", "明确"]
    cautious_words = ["可能", "或者", "考虑", "如果", "也许"]
    decisive_s = sum(combined.count(w) for w in decisive_words)
    cautious_s = sum(combined.count(w) for w in cautious_words)
    if decisive_s > cautious_s * 1.5:
        persona["decision_style"] = "decisive"
    elif cautious_s > decisive_s * 1.5:
        persona["decision_style"] = "cautious"
    else:
        persona["decision_style"] = "exploratory"

    return persona


def _count_formal_markers(text: str) -> int:
    markers = ["您好", "尊敬的", "恳请", "感谢", "抱歉",
               "因此", "然而", "此外", "综上所述", "由于"]
    return sum(text.count(m) for m in markers)


def build_style_instruction(persona: dict[str, Any]) -> str:
    """根据性格标签生成 system prompt 风格指令。"""
    instructions = []
    for dim, tag in persona.items():
        instruction = STYLE_INSTRUCTIONS.get(tag)
        if instruction:
            instructions.append(instruction)
    if not instructions:
        return ""
    return "\n".join(f"- {ins}" for ins in instructions)


def save_persona(persona: dict[str, Any]) -> None:
    """将性格分析结果持久化到 profile。"""
    from app.agent.agent_data_service import read_memory, write_memory

    profile = read_memory("stable_profile")
    profile["_persona"] = persona
    profile["_persona_updated"] = datetime.now().isoformat()
    write_memory("stable_profile", profile, merge=True)
    logger.info("persona.saved", dims=list(persona.keys()))


def load_persona() -> dict[str, Any]:
    """从 profile 加载最近的性格分析。"""
    from app.agent.agent_data_service import read_memory

    profile = read_memory("stable_profile")
    persona = profile.get("_persona")
    if isinstance(persona, dict) and persona:
        return persona
    return None
