"""Agent 自评测系统 — 启动时自检 + 运行时评分。

评测维度:
  1. 数据健康 — vault 完整性、记忆丰富度、trace 活跃度
  2. 工具健康 — 工具调用次数、成功率、延迟
  3. 上下文质量 — system prompt 覆盖率、source 多样性
  4. 响应质量 — LLM-as-judge 抽样评分（可选）
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger


# ===========================================================================
# 1. 数据健康检查
# ===========================================================================

def _check_vault_health() -> dict[str, Any]:
    vault_path = Path(settings.obsidian_vault)
    if not vault_path.exists():
        return {"status": "❌", "error": "vault 路径不存在"}

    md_files = list(vault_path.rglob("*.md"))
    if not md_files:
        return {"status": "❌", "error": "vault 中没有 .md 文件"}

    total_chars = 0
    folder_counts: dict[str, int] = {}
    for f in md_files:
        try:
            total_chars += len(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
        rel = f.relative_to(vault_path)
        folder = rel.parent.name if rel.parent.name != "." else "root"
        folder_counts[folder] = folder_counts.get(folder, 0) + 1

    return {
        "status": "✅",
        "file_count": len(md_files),
        "total_chars": total_chars,
        "total_tokens_est": total_chars // 4,  # 中文约 1.5-2 char/token，取 4 保守
        "folders": folder_counts,
    }


def _check_memory_health() -> dict[str, Any]:
    agent_data = settings.agent_data_dir
    memory_dir = agent_data / "memory"
    if not memory_dir.exists():
        return {"status": "⚠️", "memory_count": 0, "note": "memory 目录不存在"}

    stats: dict[str, Any] = {"status": "✅", "memory_count": 0, "detail": {}}
    for fname in ["profile.json", "episodic.json", "task_memory.json"]:
        fp = memory_dir / fname
        if not fp.exists():
            stats["detail"][fname] = "不存在"
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if fname == "episodic.json":
                entries = data.get("entries", [])
                stats["detail"]["episodic_entries"] = len(entries)
                if entries:
                    oldest = entries[0].get("timestamp", "")[:10]
                    newest = entries[-1].get("timestamp", "")[:10]
                    stats["detail"]["episodic_range"] = f"{oldest} ~ {newest}"
            elif fname == "profile.json":
                fields = [k for k in data if not k.startswith("__")]
                stats["detail"]["profile_fields"] = fields
                stats["detail"]["profile_field_count"] = len(fields)
            elif fname == "task_memory.json":
                todos = data.get("todos", [])
                history = data.get("history", [])
                stats["detail"]["pending_tasks"] = len(todos)
                stats["detail"]["history_plans"] = len(history)
        except Exception as e:
            stats["detail"][fname] = f"解析错误: {e}"

    # 总量评分
    total_entries = 0
    for v in stats["detail"].values():
        if isinstance(v, int):
            total_entries += v
    stats["memory_count"] = total_entries
    return stats


def _check_trace_health() -> dict[str, Any]:
    traces_dir = settings.agent_data_dir / "traces"
    if not traces_dir.exists():
        return {"status": "⚠️", "trace_count": 0, "note": "无 trace 记录"}

    files = sorted(traces_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"status": "⚠️", "trace_count": 0}

    today = date.today().isoformat()
    today_count = 0
    type_counts: dict[str, int] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ts = data.get("timestamp", "")[:10]
            if ts == today:
                today_count += 1
            t = data.get("task_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        except Exception:
            pass

    return {
        "status": "✅" if len(files) > 3 else "⚠️",
        "trace_count": len(files),
        "today_traces": today_count,
        "by_type": type_counts,
    }


# ===========================================================================
# 2. 工具健康检查
# ===========================================================================

def _check_tools() -> dict[str, Any]:
    """检查工具注册情况。"""
    from app.agent.graphs.tools import get_agent_tools

    tool_list = get_agent_tools()
    tool_names = [t.name for t in tool_list]
    mcp_names = [n for n in tool_names if "_" in n and n.split("_")[0] in ("github", "browser")]
    native_names = [n for n in tool_names if n not in mcp_names]
    return {
        "status": "✅",
        "tool_count": len(tool_names),
        "native_count": len(native_names),
        "mcp_count": len(mcp_names),
        "tools": tool_names,
        "layers": {
            "native_only": {
                "vault_readonly": [n for n in native_names if n in (
                    "search_vault", "read_folder", "read_file", "get_user_profile", "vault_structure"
                )],
                "agent_data_rw": [n for n in native_names if n in (
                    "read_memory", "write_episodic_memory", "update_task_status", "get_today_state"
                )],
                "external": [n for n in native_names if n in (
                    "get_fund_data", "get_github_trending", "get_ai_news"
                )],
            },
            "github_mcp": [n for n in mcp_names if n.startswith("github_")],
            "browser_mcp": [n for n in mcp_names if n.startswith("browser_")],
        }
    }


# ===========================================================================
# 3. 上下文质量
# ===========================================================================

def _check_context_quality() -> dict[str, Any]:
    """检查 build_context 的丰富度。"""
    from app.agent.agent_data_service import build_context

    ctx = build_context(task="检查上下文质量", max_tokens=3000)
    sources = ctx.get("sources", [])
    context_text = ctx.get("context", "")

    layer_types = set()
    for s in sources:
        layer = s.get("layer", "?")
        stype = s.get("type", "?")
        layer_types.add(f"{layer}/{stype}")

    return {
        "status": "✅" if len(sources) > 1 else "⚠️",
        "context_chars": len(context_text),
        "context_tokens_est": len(context_text) // 4,
        "source_count": len(sources),
        "source_layers": sorted(layer_types),
    }


# ===========================================================================
# 4. 综合评测
# ===========================================================================

def run_self_eval() -> dict[str, Any]:
    """运行一次完整的自我评测。"""
    start = time.monotonic()
    report: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "vault": _check_vault_health(),
        "memory": _check_memory_health(),
        "traces": _check_trace_health(),
        "tools": _check_tools(),
        "context_quality": _check_context_quality(),
    }

    # 综合评分
    scores = []
    for key in ["vault", "memory", "traces", "tools", "context_quality"]:
        s = report[key]
        status = s.get("status", "❌")
        if status == "✅":
            scores.append(100)
        elif status == "⚠️":
            scores.append(50)
        else:
            scores.append(0)

    report["overall_score"] = round(sum(scores) / len(scores), 1) if scores else 0

    # 问题清单
    issues = []
    if report["vault"].get("file_count", 0) < 5:
        issues.append("vault 中文件少于 5 个，知识资产太少")
    if report["memory"].get("memory_count", 0) < 5:
        issues.append(f"记忆条目仅 {report['memory']['memory_count']} 条，需多交互积累")
    if report["traces"].get("trace_count", 0) < 3:
        issues.append("trace 记录太少，无法评估运行趋势")
    ctx_source_count = report["context_quality"].get("source_count", 0)
    if ctx_source_count < 2:
        issues.append(f"context 仅 {ctx_source_count} 个来源，建议丰富记忆层")
    if "profile.json" in report["memory"].get("detail", {}):
        issues.append("profile.json 未初始化，agent 没有你的画像记忆")

    report["issues"] = issues
    report["latency_ms"] = int((time.monotonic() - start) * 1000)

    # 存一份到 agent_data
    try:
        eval_dir = settings.agent_data_dir / "eval"
        eval_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        (eval_dir / f"{today}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return report


def print_report(report: dict[str, Any]) -> str:
    """格式化输出评测报告。"""
    lines = ["\n📊 Agent 自评测报告"]
    lines.append("=" * 50)
    lines.append(f"综合评分: **{report['overall_score']}/100**\n")

    # 各维度
    sections = [
        ("📁 Vault 健康", report.get("vault", {})),
        ("🧠 记忆丰富度", report.get("memory", {})),
        ("🔍 Trace 活跃度", report.get("traces", {})),
        ("🛠️ 工具生态", report.get("tools", {})),
        ("📐 上下文质量", report.get("context_quality", {})),
    ]
    for title, data in sections:
        status = data.get("status", "❌")
        lines.append(f"{status} **{title}**")

        for k, v in data.items():
            if k == "status":
                continue
            if isinstance(v, dict):
                lines.append(f"   ├ {k}:")
                for kk, vv in v.items():
                    if isinstance(vv, list) and len(vv) > 5:
                        lines.append(f"   │  ├ {kk}: {vv[:5]}... (+{len(vv)-5})")
                    else:
                        lines.append(f"   │  ├ {kk}: {vv}")
            elif isinstance(v, list) and len(v) > 5:
                lines.append(f"   ├ {k}: {v[:5]}... (+{len(v)-5})")
            else:
                lines.append(f"   ├ {k}: {v}")
        lines.append("")

    # 问题
    issues = report.get("issues", [])
    if issues:
        lines.append("⚠️ **待改进项**")
        for i, issue in enumerate(issues, 1):
            lines.append(f"  {i}. {issue}")
    else:
        lines.append("✅ **一切正常，无需改进**")

    lines.append(f"\n⏱ 评测耗时: {report.get('latency_ms', 0)}ms")
    lines.append("=" * 50)

    return "\n".join(lines)
