"""Meta — 元进化层（P1）：进化系统自身参数与蒸馏 prompt 的自我迭代。

三层闭环（L1 memory / L2 policy / L3 tool）之上的一层：不训练权重，
把"进化怎么进化"变成可版本化、可回滚的资产：
  - 数值参数（批大小/阈值/窗口等）在 PARAM_BOUNDS 硬区间内自动调整（白名单语义）
  - 蒸馏 prompt 版本化：meta_review 产出新版本自动试行，统计劣化自动回退
  - 全部变更写 ledger（meta_update / meta_prompt_rollback），配合快照可整体回滚

依赖方向（避免循环 import）：
  meta → experience（state/traces）；update/distill → meta；meta 不 import update。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.agent.graphs.llm import get_chat_model
from app.core.config import settings
from app.core.logging import logger

from . import experience as exp

_META_CONFIG = settings.agent_data_dir / "evolution" / "meta_config.json"
_META_STATS = settings.agent_data_dir / "evolution" / "meta_stats.json"
_PROMPTS_DIR = settings.agent_data_dir / "evolution" / "prompts"

# ── 可调参数（默认值 = 各模块现有常量，向后兼容）──────────────────────────
DEFAULT_PARAMS: dict[str, Any] = {
    "min_distill_traces": 5,        # experience.MIN_DISTILL_TRACES
    "max_distill_input": 12,        # experience.MAX_DISTILL_INPUT
    "max_policies": 12,             # update.MAX_POLICIES
    "promote_threshold": 3,         # update.PROMOTE_THRESHOLD
    "retire_threshold": -2,         # update.RETIRE_THRESHOLD
    "recent_window": 5,             # experience.RECENT_WINDOW
    "auto_evolve_interval_h": 12,   # experience.AUTO_EVOLVE_MIN_INTERVAL_H
    "evolution_block_chars": 900,   # update.build_evolution_block max_chars
    "policy_ab_gate": True,         # P2：策略固化 skill 前是否跑 A/B 门
    "ab_max_cases": 6,              # A/B 门每阶段用例数上限
    "meta_review_every_n_distills": 5,
    "meta_prompt_eval_runs": 3,     # candidate prompt 跑满 N 次蒸馏后评估去留
}

# 数值参数的硬区间：meta 只能在区间内自动调参，越界 clamp
PARAM_BOUNDS: dict[str, tuple[int, int]] = {
    "min_distill_traces": (3, 10),
    "max_distill_input": (6, 16),
    "max_policies": (8, 20),
    "promote_threshold": (2, 5),
    "retire_threshold": (-4, -1),
    "recent_window": (3, 10),
    "auto_evolve_interval_h": (6, 48),
    "evolution_block_chars": (500, 1500),
    "meta_review_every_n_distills": (3, 20),
    "meta_prompt_eval_runs": (2, 6),
    "ab_max_cases": (4, 10),
}

# bool 型可调参数（白名单之外一律拒绝自动修改）
BOOL_PARAMS = {"policy_ab_gate"}


# ── 蒸馏 prompt 资产 v1（文件丢失时的 seed 与兜底文本）───────────────────
DISTILL_PROMPT_V1_ASSET = """You are the reflection engine of a personal AI agent. Distill raw execution traces into durable experience.

## Input: recent task executions (newest last)
{traces}

## Task
Analyze patterns: repeated tasks, recurring mistakes, inefficiencies (slow paths, unnecessary tool calls, repeated clarification), and what worked well. Traces may carry failure_codes (error taxonomy tags); use them to spot systematic weaknesses instead of one-off noise.

## Output — JSON only:
{{
  "experiences": [
    {{
      "title": "short title",
      "category": "lessons|decisions",
      "content": "one or two sentences, concrete and actionable",
      "tags": ["tag1"]
    }}
  ],
  "policy_suggestions": [
    {{
      "task_type": "chatbot|plan|reflect|memory|daily_plan",
      "trigger": "condition that should activate this policy",
      "triggers": ["2-4 short keywords (1-3 words, lowercase) likely to appear verbatim in future task text"],
      "action": "exact behavioral change, one sentence, imperative",
      "benefit": "expected improvement: latency/tokens/success"
    }}
  ],
  "tool_requests": [
    {{
      "name": "snake_case_name",
      "description": "what the missing tool should do",
      "input_schema": {{"type": "object", "properties": {{}}, "required": []}},
      "reason": "evidence from traces: which task was blocked and why existing tools are insufficient"
    }}
  ]
}}

Rules:
- experiences: at most 4. Prefer NEW insights; skip trivia.
- policy_suggestions: at most 2, only when the traces show a clear repeated pattern with a concrete fix. Each must be a change the agent can actually follow next time.
- triggers keywords must be SHORT and generic enough to actually match future tasks (e.g. "weather", "meeting notes"); never full sentences.
- tool_requests: at most 1. Only when traces clearly show a MISSING TOOL (task blocked, repeated manual workaround, or a capability no existing tool covers). Do NOT request tools for one-off tasks or information lookups. Leave the array empty otherwise.
- Do not invent metrics. Use only what the traces show.
"""

META_PROMPT = """You are the meta-evolution tuner of a self-improving personal AI agent. Your job: adjust the EVOLUTION SYSTEM's own parameters (and optionally its distillation prompt) based on evidence. You are not solving user tasks.

## Current evolution parameters
{params}

## Tunable parameter bounds (values are clamped to these)
{bounds}

## Distillation performance per prompt version
{stats}

## Recent failure distribution (error taxonomy)
{failures}

## Rules
- Only propose changes supported by the evidence above. Small steps: at most 3 parameter changes per review.
- Interpret the signals: high parse_fail rate or thin yield means the distillation prompt needs revision; systematic failure codes (e.g. many WRONG_TOOL) may justify lowering promote_threshold (固化更快) or shrinking max_distill_input (反思更聚焦).
- Only revise the distillation prompt when parse failures are frequent or outputs are consistently thin; otherwise return null.
- The new prompt MUST keep the {{traces}} placeholder, the JSON skeleton (experiences/policy_suggestions/tool_requests with triggers keywords), and all Rules about limits.

## Output — JSON only:
{{
  "param_changes": [
    {{"name": "param_name", "value": 8, "reason": "one-line evidence-based reason"}}
  ],
  "prompt_revision": null
}}
"""


# ---------------------------------------------------------------------------
# 配置读写（load 时 clamp，写只在显式变更时发生）
# ---------------------------------------------------------------------------

def _clamp_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k in BOOL_PARAMS:
            out[k] = bool(v)
        elif k in PARAM_BOUNDS:
            lo, hi = PARAM_BOUNDS[k]
            try:
                out[k] = max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                out[k] = DEFAULT_PARAMS.get(k)
        else:
            out[k] = v  # 未知键原样保留（向前兼容人工编辑）
    return out


def _load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "schema_version": 1, "revision": 0,
        "params": dict(DEFAULT_PARAMS),
        "prompt": {"distill_version": "v1"},
        "updated_at": "",
    }
    if _META_CONFIG.exists():
        try:
            data = json.loads(_META_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg["params"] = _clamp_params({**cfg["params"], **(data.get("params") or {})})
                cfg["revision"] = int(data.get("revision", 0))
                if isinstance(data.get("prompt"), dict):
                    cfg["prompt"] = data["prompt"]
                cfg["updated_at"] = str(data.get("updated_at", ""))
        except (json.JSONDecodeError, OSError, ValueError):
            pass  # 坏文件 → 全默认值继续跑
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    _META_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    _META_CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_param(name: str, default: Any = None) -> Any:
    """读一个进化参数（带 clamp 与缺省回退）。"""
    return _load_config()["params"].get(name, DEFAULT_PARAMS.get(name, default))


def set_params(changes: dict[str, Any], reason: str = "") -> dict[str, Any]:
    """白名单内改参（clamp 后生效），写 ledger。未知参数一律拒绝。"""
    cfg = _load_config()
    applied: dict[str, Any] = {}
    for k, v in changes.items():
        if k in BOOL_PARAMS:
            applied[k] = bool(v)
        elif k in PARAM_BOUNDS:
            lo, hi = PARAM_BOUNDS[k]
            try:
                applied[k] = max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                continue
    if not applied:
        return {"applied": {}}
    cfg["params"].update(applied)
    cfg["revision"] = int(cfg.get("revision", 0)) + 1
    cfg["updated_at"] = datetime.now().isoformat()
    _save_config(cfg)
    from . import ledger
    ledger.log_event("meta_update", kind="params", changes=applied,
                     reason=str(reason)[:300], revision=cfg["revision"])
    return {"applied": applied, "revision": cfg["revision"]}


# ---------------------------------------------------------------------------
# 蒸馏 prompt 资产管理
# ---------------------------------------------------------------------------

def load_distill_prompt() -> tuple[str, str]:
    """返回 (prompt 模板, 版本号)。文件缺失时自动 seed v1。"""
    cfg = _load_config()
    version = str((cfg.get("prompt") or {}).get("distill_version") or "v1")
    path = _PROMPTS_DIR / f"distill_{version}.txt"
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DISTILL_PROMPT_V1_ASSET, encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        if "{traces}" in text:
            return text, version
    except OSError:
        pass
    return DISTILL_PROMPT_V1_ASSET, version  # 文件损坏 → 内置兜底


def _install_prompt_revision(new_text: str, rationale: str = "") -> dict[str, Any]:
    """安装新版本蒸馏 prompt：健全性校验 → 落盘 → 切换活跃版本（自动试行）。"""
    if (len(new_text) < 200 or "{traces}" not in new_text
            or "policy_suggestions" not in new_text):
        return {"installed": False,
                "reason": "prompt 健全性校验未通过（长度/{traces}占位/JSON 骨架）"}
    cfg = _load_config()
    cur = str((cfg.get("prompt") or {}).get("distill_version") or "v1")
    cur_n = int(cur[1:]) if cur[1:].isdigit() else 1
    existing: list[int] = [cur_n]
    if _PROMPTS_DIR.is_dir():
        for p in _PROMPTS_DIR.glob("distill_v*.txt"):
            tail = p.stem.rsplit("_v", 1)[-1]
            if tail.isdigit():
                existing.append(int(tail))
    version = f"v{max(existing) + 1}"

    _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    (_PROMPTS_DIR / f"distill_{version}.txt").write_text(new_text, encoding="utf-8")
    cfg["prompt"] = {"distill_version": version, "previous": cur,
                     "switched_at": datetime.now().isoformat(), "judged": False}
    cfg["revision"] = int(cfg.get("revision", 0)) + 1
    cfg["updated_at"] = datetime.now().isoformat()
    _save_config(cfg)
    from . import ledger
    ledger.log_event("meta_update", kind="prompt", version=version, previous=cur,
                     rationale=str(rationale)[:300])
    return {"installed": True, "version": version, "previous": cur}


# ---------------------------------------------------------------------------
# 蒸馏统计 + prompt 自动回退
# ---------------------------------------------------------------------------

def _load_stats() -> dict[str, Any]:
    if _META_STATS.exists():
        try:
            data = json.loads(_META_STATS.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("versions", {})
                data.setdefault("history", [])
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"versions": {}, "history": []}


def _save_stats(stats: dict[str, Any]) -> None:
    _META_STATS.parent.mkdir(parents=True, exist_ok=True)
    _META_STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def record_distill_outcome(version: str, ok: bool, *, experiences: int = 0,
                           suggestions: int = 0, tool_requests: int = 0) -> None:
    """记录一次蒸馏结果到 meta_stats（按 prompt 版本聚合），并尝试自动回退。"""
    stats = _load_stats()
    v = stats["versions"].setdefault(version, {
        "runs": 0, "parse_fail": 0, "experiences": 0,
        "suggestions": 0, "tool_requests": 0, "first_at": "", "last_at": "",
    })
    v["runs"] += 1
    if not ok:
        v["parse_fail"] += 1
    v["experiences"] += int(experiences)
    v["suggestions"] += int(suggestions)
    v["tool_requests"] += int(tool_requests)
    now = datetime.now().isoformat()
    v["last_at"] = now
    if not v["first_at"]:
        v["first_at"] = now
    stats["history"].append({"ts": now, "version": version, "ok": bool(ok),
                             "experiences": int(experiences),
                             "suggestions": int(suggestions)})
    del stats["history"][:-200]  # 滚动保留
    _save_stats(stats)
    try:
        _maybe_auto_rollback_prompt(stats)
    except Exception as exc:
        logger.error("evolve.meta.rollback_check_failed", error=str(exc)[:200])


def _version_rate(v: dict[str, Any]) -> tuple[float, float]:
    """(parse_fail 率, 产出/次)。"""
    runs = max(int(v.get("runs", 0)), 1)
    pfr = int(v.get("parse_fail", 0)) / runs
    yield_per_run = (int(v.get("experiences", 0)) + int(v.get("suggestions", 0))
                     + int(v.get("tool_requests", 0))) / runs
    return round(pfr, 3), round(yield_per_run, 3)


def _maybe_auto_rollback_prompt(stats: dict[str, Any]) -> None:
    """candidate prompt 跑满 N 次后：与 previous 版本比，劣化则自动回退。"""
    cfg = _load_config()
    p = cfg.get("prompt") or {}
    cur, prev = str(p.get("distill_version", "")), str(p.get("previous", ""))
    if not cur or not prev or p.get("judged"):
        return
    cur_v, prev_v = stats["versions"].get(cur), stats["versions"].get(prev)
    if not cur_v or not prev_v:
        return
    eval_runs = int(get_param("meta_prompt_eval_runs", DEFAULT_PARAMS["meta_prompt_eval_runs"]))
    if int(cur_v.get("runs", 0)) < eval_runs or int(prev_v.get("runs", 0)) < 1:
        return

    cur_pfr, cur_yield = _version_rate(cur_v)
    prev_pfr, prev_yield = _version_rate(prev_v)
    bad_parse = cur_pfr >= 0.5 and cur_pfr > prev_pfr
    bad_yield = prev_yield > 0 and cur_yield < 0.5 * prev_yield
    if not (bad_parse or bad_yield):
        p["judged"] = True  # 通过考验：正式接受该版本
        cfg["prompt"] = p
        _save_config(cfg)
        return

    # 回退：恢复 previous 为活跃版本（保留版本文件供人工检查）
    cfg["prompt"] = {"distill_version": prev, "judged": True,
                     "rolled_back_from": cur,
                     "reason": f"parse_fail {cur_pfr} vs {prev_pfr}, "
                               f"yield {cur_yield} vs {prev_yield}",
                     "switched_at": datetime.now().isoformat()}
    cfg["revision"] = int(cfg.get("revision", 0)) + 1
    cfg["updated_at"] = datetime.now().isoformat()
    _save_config(cfg)
    from . import ledger
    ledger.log_event("meta_prompt_rollback", frm=cur, to=prev,
                     parse_fail=cur_pfr, yield_ratio=(
                         round(cur_yield / prev_yield, 2) if prev_yield else None))
    logger.warning("evolve.meta.prompt_rolled_back", frm=cur, to=prev)


def _stats_summary(stats: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ver, v in stats.get("versions", {}).items():
        pfr, ypr = _version_rate(v)
        out[ver] = {"runs": v.get("runs", 0), "parse_fail_rate": pfr,
                    "yield_per_run": ypr}
    return out


def _failures_summary(dist: dict[str, Any]) -> dict[str, Any]:
    if not dist:
        return {"note": "no failure data"}
    by_code = dist.get("by_code") or {}
    top = dict(sorted(by_code.items(), key=lambda kv: -kv[1])[:8])
    return {"failure_rate": dist.get("failure_rate"),
            "top_codes": top,
            "by_category": dist.get("by_category") or {}}


# ---------------------------------------------------------------------------
# meta 复审（evolve_now 末尾调用，内部节流）
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned).rstrip("` \n")
    return json.loads(cleaned)


def meta_review(force: bool = False) -> dict[str, Any]:
    """元进化复审：蒸馏统计 + 失败分布 → LLM 建议 → 白名单内应用。

    节流：距上次复审的蒸馏批数 < meta_review_every_n_distills 时跳过。
    """
    state = exp.load_state()
    distill_count = int(state.get("distill_count", 0))
    every = int(get_param("meta_review_every_n_distills",
                          DEFAULT_PARAMS["meta_review_every_n_distills"]))
    last_count = int(state.get("last_meta_review_distill_count", -1))
    if not force and distill_count - last_count < every:
        return {"success": True, "skipped": True,
                "reason": f"距上次复审仅蒸馏 {max(distill_count - last_count, 0)}/{every} 批"}

    cfg = _load_config()
    stats_summary = _stats_summary(_load_stats())
    failure_dist: dict[str, Any] = {}
    try:
        from app.agent.failure_taxonomy import compute_failure_distribution
        failure_dist = compute_failure_distribution(exp.load_traces(limit=100))
    except Exception:
        pass

    try:
        model = get_chat_model(temperature=0.2)
        prompt = META_PROMPT.format(
            params=json.dumps(cfg["params"], ensure_ascii=False),
            bounds=json.dumps({**{k: list(v) for k, v in PARAM_BOUNDS.items()},
                               **{k: "bool" for k in BOOL_PARAMS}},
                              ensure_ascii=False),
            stats=json.dumps(stats_summary, ensure_ascii=False),
            failures=json.dumps(_failures_summary(failure_dist), ensure_ascii=False),
        )
        response = model.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        advice = _parse_json(text)
    except Exception as exc:
        state["last_meta_review_distill_count"] = distill_count
        state["last_meta_review_at"] = datetime.now().isoformat()
        exp.save_state(state)
        logger.error("evolve.meta.llm_failed", error=str(exc)[:200])
        return {"success": False, "error": f"meta LLM 调用失败: {exc}"}

    # 应用参数建议（白名单 + clamp 在 set_params 内）
    applied_params: dict[str, Any] = {}
    changes_raw = advice.get("param_changes") or []
    if isinstance(changes_raw, list) and changes_raw:
        changes = {str(c.get("name", "")): c.get("value")
                   for c in changes_raw if isinstance(c, dict) and c.get("name")}
        reasons = "; ".join(str(c.get("reason", ""))[:80] for c in changes_raw
                            if isinstance(c, dict) and c.get("name") in changes)
        if changes:
            applied_params = set_params(changes, reason=reasons).get("applied", {})

    # 应用 prompt 修订（健全性校验在 _install_prompt_revision 内）
    prompt_result = None
    rev = advice.get("prompt_revision")
    if isinstance(rev, dict) and rev.get("new_text"):
        prompt_result = _install_prompt_revision(
            str(rev["new_text"]), str(rev.get("rationale", "")))

    state["last_meta_review_distill_count"] = distill_count
    state["last_meta_review_at"] = datetime.now().isoformat()
    exp.save_state(state)
    return {"success": True, "skipped": False,
            "applied": applied_params, "prompt": prompt_result}


def meta_status() -> dict[str, Any]:
    """CLI `evolve meta` 用：配置 + 统计 + prompt 版本历史。"""
    cfg = _load_config()
    stats = _load_stats()
    prompt_files = sorted(p.stem + ".txt" for p in _PROMPTS_DIR.glob("distill_v*.txt")) \
        if _PROMPTS_DIR.is_dir() else []
    state = exp.load_state()
    return {
        "config": cfg,
        "stats": _stats_summary(stats),
        "prompt_files": prompt_files,
        "last_meta_review_at": state.get("last_meta_review_at", ""),
        "distill_count": state.get("distill_count", 0),
    }
