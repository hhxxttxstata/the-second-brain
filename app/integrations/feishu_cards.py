"""飞书消息卡片构建器 — 把 Agent 输出渲染成飞书消息卡片。

支持文本消息和 Interactive Card 两种格式。
"""

from __future__ import annotations

import json
from typing import Any


def text_message(text: str) -> dict[str, Any]:
    """纯文本消息（适合简单回复）。"""
    return {
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }


def plan_card(plan_data: dict[str, Any]) -> dict[str, Any]:
    """把 Daily Plan 渲染为飞书消息卡片。"""
    items = plan_data.get("items", [])
    lines = []
    for i, item in enumerate(items, 1):
        icon = {"diary_todo": "📓", "pending_task": "🔄", "signal": "📡",
                "stable_profile": "🎯", "default": "•"}.get(item.get("source", ""), "•")
        lines.append(f"{i}. {icon} **[{item.get('priority', 'MEDIUM')}]** {item.get('title', '')}")
        if item.get("description"):
            lines.append(f"   {item['description'][:60]}")

    content = "\n".join(lines) if lines else "暂无计划项"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 今日计划 — {plan_data.get('date', '')}"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": f"基于 {plan_data.get('context_count', 0)} 个数据资产生成 | ⏱ {plan_data.get('latency_ms', 0)}ms"}
                ]},
            ],
        },
    }


def steward_card(report: dict[str, Any]) -> dict[str, Any]:
    """把 Steward 巡检报告渲染为消息卡片。"""
    findings = report.get("findings", [])

    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "info")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    lines = []
    if severity_counts.get("high"):
        lines.append(f"🔴 高危: {severity_counts['high']}")
    if severity_counts.get("medium"):
        lines.append(f"🟡 中危: {severity_counts['medium']}")
    if severity_counts.get("low"):
        lines.append(f"🟢 低危: {severity_counts['low']}")

    # Top 5 findings
    top_findings = findings[:5]
    for f in top_findings:
        detail = f.get("detail", "")[:60]
        lines.append(f"• [{f.get('type', '?')}] {detail}")

    if len(findings) > 5:
        lines.append(f"...及其他 {len(findings) - 5} 项")

    content = "\n".join(lines) if lines else "未发现异常"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🛡️ 数据资产巡检 — {report.get('date', '')}"},
                "template": "red" if severity_counts.get("high") else "orange",
            },
            "elements": [
                {"tag": "markdown", "content": f"**总览**：{report.get('total_assets', 0)} 个资产，{report.get('total_findings', 0)} 个发现项"},
                {"tag": "div", "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"📊 总资产：{report.get('total_assets', 0)}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"🔍 发现项：{report.get('total_findings', 0)}"}},
                ]},
                {"tag": "hr"},
                {"tag": "markdown", "content": content},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": f"⏱ {report.get('latency_ms', 0)}ms | report_id: {report.get('report_id', '')}"}
                ]},
            ],
        },
    }


def search_card(result: dict[str, Any]) -> dict[str, Any]:
    """把知识搜索结果显示为卡片。"""
    items = result.get("results", [])
    if not items:
        return text_message(f"🔎 未找到与「{result.get('query', '')}」相关的结果")

    lines = [f"🔎 **搜索**: {result.get('query', '')}", f"共 {len(items)} 条结果\n"]
    for i, r in enumerate(items, 1):
        lines.append(f"{i}. [{r.get('score', 0):.3f}] {r.get('text', '')[:80]}")
        lines.append(f"   📁 {r.get('source_file', '')}")
        lines.append("")

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🔎 知识检索结果"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "发送「plan」生成计划 · 「steward」巡检 · 「search <关键词>」搜索"}
                ]},
            ],
        },
    }


def status_card(metrics: dict[str, Any]) -> dict[str, Any]:
    """系统状态卡片。"""
    a = metrics.get("asset_metrics", {})
    ag = metrics.get("agent_metrics", {})
    g = metrics.get("governance_metrics", {})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "📊 系统状态"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"📦 总资产：{a.get('total_assets', '?')}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"⭐ 平均质量：{a.get('avg_quality_score', '?')}"}},
                    ],
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"🤖 Agent 调用：{ag.get('total_traces', '?')}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"📈 工具成功率：{ag.get('tool_success_rate', '?')}%"}},
                    ],
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"🔗 血缘完整率：{g.get('lineage_completeness', '?')}%"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"📋 计划采纳率：{ag.get('plan_adoption_rate', '?')}%"}},
                    ],
                },
            ],
        },
    }


def error_card(error_msg: str) -> dict[str, Any]:
    """错误提示卡片。"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 操作失败"},
                "template": "red",
            },
            "elements": [
                {"tag": "markdown", "content": f"错误：{error_msg}"},
            ],
        },
    }


def help_card() -> dict[str, Any]:
    """帮助卡片。"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 Agent 助手"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": """**支持的命令：**

**plan** — 生成今日进步计划
**steward** — 数据资产巡检报告
**search <关键词>** — 搜索知识库
**status** — 查看系统状态
**help** — 显示此帮助

你也可以直接提问，我会帮你搜索相关知识库。"""},
            ],
        },
    }


COMMAND_DESCRIPTIONS = {
    "plan": "生成今日计划",
    "steward": "数据资产巡检",
    "search": "搜索知识库",
    "status": "系统状态",
    "help": "帮助",
}
