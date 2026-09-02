"""Runner — 自进化编排：手动触发、自动触发（节流 + 后台线程）、状态查看。

流水线: 蒸馏(reflect 批量模式) → 应用(policy) → 验证(skill 固化/回滚)
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.core.logging import logger

from . import experience as exp
from . import update


def evolve_now(force: bool = False, dry_run: bool = False,
               background: bool = False) -> dict[str, Any]:
    """跑一轮完整闭环：蒸馏 + 策略验证（+ meta 复审）。

    background=True（自动进化 daemon 线程）时，A/B 晋升门走子进程，
    避免 isolate_agent_data 的全局路径 patch 与在线请求竞态。
    """
    from . import distill, ledger

    report: dict[str, Any] = {"force": force, "dry_run": dry_run}

    # 治理：先落一份 harness 快照，再动可变状态（可整体回滚）
    snap_id = None if dry_run else ledger.take_snapshot("pre")

    d = distill.distill_once(force=force, dry_run=dry_run)
    report["distill"] = d
    if d.get("skipped") and not force:
        # 没新 trace 时也做一次策略验证（可能已有策略在等待评分）
        pass
    if d.get("success") or force:
        try:
            report["update"] = update.evaluate_policies(
                ab_mode="spawn" if background else "run")
        except Exception as exc:
            report["update"] = {"success": False, "error": str(exc)}
            logger.error("evolve.update_failed", error=str(exc)[:200])

    # meta 复审（进化参数/prompt 的自我迭代；节流在 meta_review 内部）
    try:
        from . import meta
        report["meta"] = meta.meta_review()
    except Exception as exc:
        report["meta"] = {"success": False, "error": str(exc)[:200]}
        logger.error("evolve.meta_failed", error=str(exc)[:300])

    if not dry_run:
        ledger.log_event(
            "evolve_run", force=force, snapshot=snap_id,
            distill_ok=bool(d.get("success")),
            experiences=d.get("experiences", 0),
            policy_suggestions=d.get("policy_suggestions", 0),
            evaluated=report.get("update", {}).get("evaluated", 0),
            promoted=len(report.get("update", {}).get("promoted", []) or []),
            retired=len(report.get("update", {}).get("retired", []) or []),
            meta_updated=bool(report.get("meta", {}).get("applied")),
        )
    return report


def maybe_auto_evolve() -> dict[str, Any]:
    """自动触发检查：间隔足够 + 新 trace 足够 → 后台线程执行。

    同步返回，不阻塞调用方；实际蒸馏在 daemon 线程中运行。
    节流阈值来自 meta 层（可自进化调整）。
    """
    from . import meta

    state = exp.load_state()
    now = datetime.now()

    # 1. 间隔节流
    interval_h = int(meta.get_param("auto_evolve_interval_h", exp.AUTO_EVOLVE_MIN_INTERVAL_H))
    last = state.get("last_auto_evolve_at", "")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=interval_h):
                return {"triggered": False, "reason": "间隔未到"}
        except ValueError:
            pass

    # 2. 新 trace 数量
    min_traces = int(meta.get_param("min_distill_traces", exp.MIN_DISTILL_TRACES))
    traces = exp.load_traces(limit=200)
    undistilled = exp.get_undistilled(traces, state)
    if len(undistilled) < min_traces:
        return {"triggered": False, "reason": f"新 trace 不足（{len(undistilled)}/{min_traces}）"}

    # 3. 立即占位（防止并发重复触发），再后台执行
    state["last_auto_evolve_at"] = now.isoformat()
    state["auto_evolve_count"] = int(state.get("auto_evolve_count", 0)) + 1
    exp.save_state(state)

    def _background() -> None:
        try:
            report = evolve_now(force=False, background=True)
            logger.info("evolve.auto.done", distill=report.get("distill", {}).get("success"),
                        update=report.get("update", {}).get("evaluated"))
        except Exception as exc:
            logger.error("evolve.auto.failed", error=str(exc)[:300])

    threading.Thread(target=_background, daemon=True, name="agent-evolve").start()
    return {"triggered": True, "undistilled": len(undistilled),
            "note": "蒸馏已在后台启动"}


def status() -> dict[str, Any]:
    """汇总状态（CLI `evolve status` 用）。"""
    tools_summary: dict[str, Any] = {}
    try:
        from app.tool_registry.dynamic_tools import summary as tools_summary_fn
        tools_summary = tools_summary_fn()
    except Exception as exc:
        logger.error("evolve.status.tools_failed", error=str(exc)[:200])
        tools_summary = {"count": 0, "names": [], "pending_count": 0, "pending": []}
    roi: dict[str, Any] = {}
    try:
        from .roi import collect_roi
        roi = collect_roi()
    except Exception as exc:
        logger.error("evolve.status.roi_failed", error=str(exc)[:200])
    return {
        "experience": exp.status_summary(),
        "policies": update.policy_summary(),
        "skills": update.list_skills(),
        "dynamic_tools": tools_summary,
        "roi": roi,
    }
