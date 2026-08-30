"""Update — L2 策略应用层：把蒸馏出的建议变成可执行的 policy / skill。

职责：
  - apply_policy_suggestions: 新建议 → proposed 策略（写 policies.json + policies.md）
  - evaluate_policies: 用窗口对比验证 active 策略的效果
      improved → score+1；regressed → score-1；无信号 → 不变
      score ≥ 3 → 固化为 skill（skills/<name>.md + MEMORY.md 索引）
      score ≤ -2 → 退役（retired，不再注入）
  - build_evolution_block: 每轮上下文注入（policies + 命中 trigger 的 skills）
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

from . import experience as exp

_POLICIES_JSON = settings.agent_data_dir / "evolution" / "policies.json"
_POLICIES_MD = settings.agent_data_dir / "memory" / "policies.md"
_SKILLS_DIR = settings.agent_data_dir / "memory" / "skills"

PROMOTE_THRESHOLD = 3    # score 达到 → 固化为 skill
RETIRE_THRESHOLD = -2    # score 低到 → 退役
MAX_POLICIES = 12        # 策略数量上限（防膨胀）


def _load_policies() -> list[dict[str, Any]]:
    if not _POLICIES_JSON.exists():
        return []
    try:
        data = json.loads(_POLICIES_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_policies(policies: list[dict[str, Any]]) -> None:
    _POLICIES_JSON.parent.mkdir(parents=True, exist_ok=True)
    _POLICIES_JSON.write_text(
        json.dumps(policies, ensure_ascii=False, indent=2), encoding="utf-8")
    _sync_policies_md(policies)


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip().lower())
    return slug[:48].strip("-") or f"policy-{uuid.uuid4().hex[:6]}"


def _sync_policies_md(policies: list[dict[str, Any]]) -> None:
    """把策略同步为 policies.md（每轮注入上下文的来源）。"""
    _POLICIES_MD.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Policies", "",
             "_自进化策略 — 由 reflect→update 闭环生成，衡量有效后固化为 skill。_", ""]
    active = [p for p in policies if p.get("status") in ("proposed", "active")]
    if not active:
        lines.append("（暂无活动策略）")
    for p in active:
        status = "✅" if p.get("status") == "active" else "⏳"
        lines.append(f"- {status} [{p['task_type']}] 触发: {p.get('trigger', '')} "
                     f"| 动作: {p.get('action', '')} | 收益: {p.get('benefit', '')}")
    _POLICIES_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 应用：建议 → 策略
# ---------------------------------------------------------------------------

def apply_policy_suggestions(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    """把蒸馏建议落为 proposed 策略。已存在同型策略则只刷新 last_seen。"""
    policies = _load_policies()
    added = 0
    refreshed = 0
    now = datetime.now().isoformat()

    for s in suggestions:
        task_type = str(s.get("task_type", "")).strip() or "chatbot"
        trigger = str(s.get("trigger", "")).strip()[:80]
        action = str(s.get("action", "")).strip()[:300]
        benefit = str(s.get("benefit", "")).strip()[:120]
        if not action:
            continue

        # 查重：同 task_type 且动作前 24 字相似
        dup = next((p for p in policies
                    if p.get("task_type") == task_type
                    and p.get("action", "")[:24] == action[:24]), None)
        if dup:
            dup["last_seen_at"] = now
            if dup.get("status") == "retired":
                dup["status"] = "proposed"
                dup["score"] = 0
                refreshed += 1
            continue

        policies.append({
            "policy_id": f"pol_{uuid.uuid4().hex[:8]}",
            "task_type": task_type,
            "trigger": trigger,
            "action": action,
            "benefit": benefit,
            "status": "proposed",
            "score": 0,
            "metrics": {},
            "created_at": now,
            "last_seen_at": now,
        })
        added += 1

    # 上限保护：退役最旧的低分策略
    if len(policies) > MAX_POLICIES:
        candidates = [p for p in policies if p.get("status") != "active"]
        candidates.sort(key=lambda p: (p.get("score", 0), p.get("created_at", "")))
        for p in candidates[:len(policies) - MAX_POLICIES]:
            p["status"] = "retired"

    _save_policies(policies)
    logger.info("evolve.update.applied", added=added, refreshed=refreshed,
                total=len(policies))
    return {"success": True, "added": added, "refreshed": refreshed,
            "total": len(policies)}


# ---------------------------------------------------------------------------
# 验证：策略效果度量 → 提升 / 回滚
# ---------------------------------------------------------------------------

def evaluate_policies() -> dict[str, Any]:
    """对每条非 retired 策略做窗口对比，更新 score 并执行 promote/retire。"""
    policies = _load_policies()
    if not policies:
        return {"success": True, "evaluated": 0, "promoted": [], "retired": []}

    traces = exp.load_traces(limit=200)
    evaluated, promoted, retired = 0, [], []

    for p in policies:
        if p.get("status") == "retired":
            continue
        cmp = exp.compare_windows(traces, p.get("task_type", ""))
        if cmp["verdict"] == "insufficient":
            continue
        evaluated += 1
        p["metrics"] = {
            "verdict": cmp["verdict"],
            "latency_delta_pct": cmp.get("latency_delta_pct"),
            "tokens_delta_pct": cmp.get("tokens_delta_pct"),
            "success_delta": cmp.get("success_delta"),
            "checked_at": datetime.now().isoformat(),
        }
        if cmp["verdict"] == "improved":
            p["score"] = int(p.get("score", 0)) + 1
        elif cmp["verdict"] == "regressed":
            p["score"] = int(p.get("score", 0)) - 1

        if int(p.get("score", 0)) >= PROMOTE_THRESHOLD:
            if _promote_to_skill(p):
                promoted.append(p.get("policy_id"))
        elif int(p.get("score", 0)) <= RETIRE_THRESHOLD:
            p["status"] = "retired"
            p["retired_at"] = datetime.now().isoformat()
            retired.append(p.get("policy_id"))

    _save_policies(policies)
    logger.info("evolve.update.evaluated", evaluated=evaluated,
                promoted=len(promoted), retired=len(retired))
    return {"success": True, "evaluated": evaluated,
            "promoted": promoted, "retired": retired}


def _promote_to_skill(policy: dict[str, Any]) -> bool:
    """策略连续有效 → 固化为 skill 文件 + MEMORY.md 索引。"""
    try:
        from ..topic_memory import upsert_index_entry
        name = _slug(policy.get("action", "")[:20] or policy["policy_id"])
        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        path = _SKILLS_DIR / f"{name}.md"
        path.write_text(
            f"""---
name: {name}
task_type: {policy['task_type']}
triggers: {policy.get('trigger', '')}
status: active
promoted_at: {date.today().isoformat()}
score: {policy.get('score', 0)}
---

# Skill: {policy.get('action', '')}

## When to use
- 触发条件: {policy.get('trigger', '')}

## Procedure
{policy.get('action', '')}

## Expected benefit
{policy.get('benefit', '')}

## Performance
- 最近验证: {json.dumps(policy.get('metrics', {}), ensure_ascii=False)}
""", encoding="utf-8")

        upsert_index_entry(
            "Skills", f"{policy['task_type']}: {policy.get('trigger', '')[:40]}",
            f"skills/{name}", policy.get('action', '')[:80],
            related=[policy["task_type"]],
        )
        policy["status"] = "active"
        policy["skill_file"] = f"skills/{name}.md"
        policy["promoted_at"] = date.today().isoformat()
        logger.info("evolve.update.promoted", policy=policy["policy_id"],
                    skill=f"skills/{name}.md")
        return True
    except Exception as exc:
        logger.error("evolve.update.promote_failed", error=str(exc)[:200])
        return False


# ---------------------------------------------------------------------------
# 注入：每轮上下文（build_context 调用）
# ---------------------------------------------------------------------------

def build_evolution_block(task: str = "", max_chars: int = 900) -> str:
    """构造 evolution 注入块：policies 摘要 + 命中 trigger 的 skills。

    由 memory_store.build_context 调用（Layer 3.5）。命中逻辑：
      1. 策略行（proposed/active 全部，截断）
      2. skills 文件：task 命中 triggers 关键词（或 task_type 匹配）才读详情
    """
    parts: list[str] = []
    budget = max_chars

    # 1. policies 摘要
    if _POLICIES_MD.exists():
        try:
            text = _POLICIES_MD.read_text(encoding="utf-8")
            active_lines = [l for l in text.split("\n")
                            if l.startswith("- ") and ("✅" in l or "⏳" in l)]
            if active_lines:
                block = "## 执行策略（自进化生成）\n" + "\n".join(active_lines[:6])
                if len(block) <= budget:
                    parts.append(block)
                    budget -= len(block)
        except OSError:
            pass

    # 2. 命中 trigger 的 skills
    if budget > 250 and _SKILLS_DIR.exists():
        task_lower = (task or "").lower()
        matched: list[Path] = []
        for f in sorted(_SKILLS_DIR.glob("*.md")):
            try:
                head = f.read_text(encoding="utf-8")[:600]
                fm = re.search(r"triggers:\s*(.+)", head)
                triggers = (fm.group(1) if fm else "").split(",")
                if any(tr.strip().lower() and tr.strip().lower() in task_lower
                       for tr in triggers):
                    matched.append(f)
                if len(matched) >= 2:
                    break
            except OSError:
                continue
        for f in matched:
            try:
                body = f.read_text(encoding="utf-8")[:500]
                block = f"## Skill: {f.stem}\n{body}"
                if len(block) <= budget:
                    parts.append(block)
                    budget -= len(block)
            except OSError:
                continue

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 状态查看
# ---------------------------------------------------------------------------

def policy_summary() -> list[dict[str, Any]]:
    return [{
        "policy_id": p.get("policy_id"),
        "task_type": p.get("task_type"),
        "trigger": p.get("trigger"),
        "action": p.get("action", "")[:60],
        "status": p.get("status"),
        "score": p.get("score", 0),
        "metrics": p.get("metrics", {}).get("verdict", ""),
    } for p in _load_policies()]


def list_skills() -> list[dict[str, Any]]:
    if not _SKILLS_DIR.exists():
        return []
    result = []
    for f in sorted(_SKILLS_DIR.glob("*.md")):
        result.append({"name": f.stem, "path": f"memory/skills/{f.name}"})
    return result
