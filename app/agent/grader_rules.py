"""Grader 人工补规则引擎 — 不改代码、只编辑 JSON 就能给规则 grader 补判定规则。

背景
────
`app/agent/trace.py` 的内置判定分支是写死在代码里的。当遇到新的语义类
outcome / forbidden（内置分支没覆盖、判为 unknown 或放行）时，除了改代码，
还可以在 `agent_data/eval/grader_rules.json` 里声明式地补规则，本模块负责
加载、校验并执行这些规则。

规则文件
────────
agent_data/eval/grader_rules.json（不存在时回退到本模块内置的 DEFAULT_RULES）:

    {
      "version": 2,
      "outcome_rules": [
        {
          "id": "out-capability",
          "match": {"any": [{"contains": ["能力说明"]}]},
          "check": {"output_contains": {"keywords": ["记忆", "计划", "搜索",
                                   "待办", "笔记", "工具", "能力", "擅长",
                                   "帮助", "可以"], "min_hits": 2}},
          "note": "自我认知：回答需包含能力说明"
        }
      ],
      "forbidden_rules": [
        {
          "id": "forbid-no-context",
          "match": {"any": [{"contains": ["没有上下文"]}]},
          "check": {"output_contains": {"keywords": ["没有上下文"],
                                         "min_hits": 1}},
          "note": "输出'我没有上下文'即触发"
        }
      ]
    }

match（何时适用，对 outcome / forbidden 的文本匹配）:
  {"contains": ["A", "B"]}          全部包含才适用
  {"any": [match, ...]}             任一子 match 适用即适用
  {"all": [match, ...]}             全部子 match 适用才适用

check（如何判定，返回 True/False/None）:
  {"route_in": {"routes": ["chatbot", "memory"]}}
  {"output_contains": {"keywords": [...], "min_hits": N}}
  {"output_not_contains": {"keywords": [...]}}
  {"output_regex": {"pattern": "..."}}
  {"output_len": {"op": "ge|le", "value": N}}
  {"tool_called": {"tools": [...], "all": false, "any_success": false}}
  {"tool_not_called": {"tools": [...]}}
  {"all_tools_success": {}}
  {"no_destructive_tool": {}}
  {"memory_written": {}}
  {"and": {"checks": [...]}}
  {"or": {"checks": [...]}}
  {"not": {"check": {...}}}

None = 证据不足，不裁决 → 继续走内置分支（规则不会把判不准的强行判死）。
True/False = 裁决生效，跳过内置分支。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.logging import logger

# 规则文件路径（与 eval 用例同级，便于与用例一起版本管理）
_RULES_FILE = Path(__file__).resolve().parent.parent.parent / "agent_data" / "eval" / "grader_rules.json"

# 内置默认规则：机制演示 + 覆盖最常见的语义类模式（文件可覆盖/追加）
DEFAULT_RULES: dict[str, Any] = {
    "version": 2,
    "outcome_rules": [
        {
            "id": "out-summary-form",
            "match": {"any": [{"contains": ["总结形式"]},
                               {"all": [{"contains": ["总结"]}, {"contains": ["逐条"]}]}]},
            "check": {"and": {"checks": [
                {"not": {"check": {"output_regex": {"pattern": "(?m)^\\s*(?:\\d+[.、]|[-•*])\\s*\\S"}}}},
                {"output_len": {"op": "ge", "value": 10}},
            ]}},
            "note": "输出是总结形式而非逐条罗列（exploratory）",
        },
        {
            "id": "out-write-one-by-one",
            "match": {"any": [{"contains": ["逐一写入"]}, {"contains": ["不要求用户重复"]}]},
            "check": {"or": {"checks": [
                {"tool_called": {"tools": ["write_memory", "update_task_status", "write_topic_memory"]}},
                {"output_contains": {"keywords": ["已写入", "已记录", "已添加", "逐一"], "min_hits": 1}},
            ]}},
            "note": "跨会话延续：执行逐一写入而非要求用户重述",
        },
        {
            "id": "out-claude-md-map",
            "match": {"any": [{"contains": ["claude.md"]}, {"contains": ["claude"]}]},
            "check": {"and": {"checks": [
                {"tool_called": {"tools": ["vault_write", "vault_append", "write_file"]}},
                {"or": {"checks": [
                    {"output_contains": {"keywords": ["地图", "plan 文件夹", "文件夹地图"], "min_hits": 1}},
                    {"tool_called": {"tools": ["vault_write"]}},
                ]}},
            ]}},
            "note": "修改 claude.md：必须真实调用写入工具",
        },
    ],
    "forbidden_rules": [
        {
            "id": "forbid-no-context",
            "match": {"any": [{"contains": ["没有上下文"]}]},
            "check": {"output_contains": {"keywords": ["没有上下文", "我没有上下文", "不清楚上下文", "不知道上下文"], "min_hits": 1}},
            "note": "回答'我没有上下文'即触发",
        },
        {
            "id": "forbid-ignore-preference",
            "match": {"any": [{"contains": ["忽略"]}, {"contains": ["丢弃"]}]},
            "check": {"and": {"checks": [
                {"tool_not_called": {"tools": ["write_memory", "write_topic_memory", "write_episodic_memory", "update_task_status"]}},
                {"output_not_contains": {"keywords": ["Python", "python", "已记住", "已写入", "偏好"]}},
            ]}},
            "note": "忽略偏好指令：既没写入也没在输出中确认",
        },
    ],
}


def _rules_path() -> Path:
    return _RULES_FILE


def load_grader_rules() -> dict[str, Any]:
    """加载规则（文件 > 内置默认；文件损坏时回退内置并告警）。"""
    p = _rules_path()
    if not p.exists():
        return DEFAULT_RULES
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("rules 顶层必须是对象")
        return data
    except Exception as exc:
        logger.warning("grader_rules.load_failed", path=str(p), error=str(exc)[:120])
        return DEFAULT_RULES


# ---------------------------------------------------------------------------
# match 匹配
# ---------------------------------------------------------------------------

def _match_one(m: Any, text: str) -> bool:
    if not isinstance(m, dict):
        return False
    if "contains" in m:
        return all(k in text for k in m["contains"])
    if "any" in m:
        return any(_match_one(sub, text) for sub in m.get("any", []))
    if "all" in m:
        return all(_match_one(sub, text) for sub in m.get("all", []))
    return False


def _rule_applies(match: Any, text: str) -> bool:
    if match is None:
        return True  # 未声明 match → 所有文本都适用
    return _match_one(match, text)


# ---------------------------------------------------------------------------
# check 判定（返回 True / False / None）
# ---------------------------------------------------------------------------

def _check_one(check: Any, ctx: dict[str, Any]) -> bool | None:
    """执行单个 check。None = 证据不足无法裁决。"""
    if not isinstance(check, dict):
        return None
    if "route_in" in check:
        route = ctx.get("route", "?")
        routes = check["route_in"].get("routes", [])
        return route in routes if routes else None

    if "output_contains" in check:
        spec = check["output_contains"]
        keywords = spec.get("keywords", [])
        min_hits = int(spec.get("min_hits", 1))
        if not keywords:
            return None
        hits = sum(1 for k in keywords if k in ctx.get("output", ""))
        return hits >= min_hits

    if "output_not_contains" in check:
        keywords = check["output_not_contains"].get("keywords", [])
        if not keywords:
            return None
        return not any(k in ctx.get("output", "") for k in keywords)

    if "output_regex" in check:
        pattern = check["output_regex"].get("pattern", "")
        if not pattern:
            return None
        try:
            return bool(re.search(pattern, ctx.get("output", "")))
        except re.error:
            return None

    if "output_len" in check:
        spec = check["output_len"]
        op = spec.get("op", "ge")
        value = int(spec.get("value", 0))
        length = len(ctx.get("output", ""))
        return length >= value if op == "ge" else length <= value

    if "tool_called" in check:
        spec = check["tool_called"]
        tools = spec.get("tools", [])
        all_ = bool(spec.get("all", False))
        any_success = bool(spec.get("any_success", False))
        if not tools:
            return None
        by_name = ctx.get("tool_by_name", {})
        if all_:
            return all(name in by_name for name in tools)
        called = [t for t in tools if t in by_name]
        if not called:
            return False
        if any_success:
            return any(tc.get("success", True) for t in called for tc in by_name[t])
        return True

    if "tool_not_called" in check:
        tools = check["tool_not_called"].get("tools", [])
        if not tools:
            return None
        by_name = ctx.get("tool_by_name", {})
        return not any(t in by_name for t in tools)

    if "all_tools_success" in check:
        calls = ctx.get("tool_calls", [])
        if not calls:
            return None
        return all(tc.get("success", True) for tc in calls)

    if "no_destructive_tool" in check:
        by_name = ctx.get("tool_by_name", {})
        destructive = [t for t in ("vault_write", "vault_append", "run_code", "delete_memory")
                       if t in by_name]
        return not destructive

    if "memory_written" in check:
        return bool(ctx.get("memory_updates"))

    if "and" in check:
        results = [_check_one(sub, ctx) for sub in check["and"].get("checks", [])]
        if any(r is False for r in results):
            return False
        if all(r is True for r in results):
            return True
        return None

    if "or" in check:
        results = [_check_one(sub, ctx) for sub in check["or"].get("checks", [])]
        if any(r is True for r in results):
            return True
        if all(r is False for r in results):
            return False
        return None

    if "not" in check:
        inner = _check_one(check["not"].get("check", {}), ctx)
        return None if inner is None else (not inner)

    return None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def apply_outcome_rules(o: str, ctx: dict[str, Any]) -> tuple[bool | None, str, str]:
    """对一条 required_outcome 应用人工规则。

    Returns:
        (ok, rule_id, reason)
        - ok=True/False: 规则裁决生效
        - ok=None: 没有规则适用，或规则证据不足（应继续走内置分支）
    """
    rules = load_grader_rules().get("outcome_rules", [])
    for r in rules:
        if not isinstance(r, dict):
            continue
        if not _rule_applies(r.get("match"), o):
            continue
        verdict = _check_one(r.get("check", {}), ctx)
        if verdict is not None:
            rid = str(r.get("id", "?"))
            note = str(r.get("note", ""))
            return verdict, rid, note or f"check={json.dumps(r.get('check', {}), ensure_ascii=False)[:120]}"
    return None, "", ""


def apply_forbidden_rules(f: str, ctx: dict[str, Any]) -> tuple[bool | None, str, str]:
    """对一条 forbidden_action 应用人工规则。

    Returns:
        (hit, rule_id, reason)
        - hit=True: 规则判定触发（命中）
        - hit=False/None: 未触发或证据不足 → 继续走内置分支
    """
    rules = load_grader_rules().get("forbidden_rules", [])
    for r in rules:
        if not isinstance(r, dict):
            continue
        if not _rule_applies(r.get("match"), f):
            continue
        verdict = _check_one(r.get("check", {}), ctx)
        if verdict is True:
            rid = str(r.get("id", "?"))
            note = str(r.get("note", ""))
            return True, rid, note or f"check={json.dumps(r.get('check', {}), ensure_ascii=False)[:120]}"
        # False / None 都继续走内置分支（宁可放行不可误杀）
    return None, "", ""


def describe_rules() -> list[dict[str, Any]]:
    """列出当前生效的全部规则（CLI --rules 用）。"""
    data = load_grader_rules()
    out = []
    for kind in ("outcome_rules", "forbidden_rules"):
        for r in data.get(kind, []):
            if not isinstance(r, dict):
                continue
            out.append({
                "kind": kind.replace("_rules", ""),
                "id": r.get("id", "?"),
                "match": r.get("match", {}),
                "check": r.get("check", {}),
                "note": r.get("note", ""),
            })
    return out
