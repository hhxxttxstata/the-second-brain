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


def evolve_now(force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """跑一轮完整闭环：蒸馏 + 策略验证。"""
    from . import distill

    report: dict[str, Any] = {"force": force, "dry_run": dry_run}

    d = distill.distill_once(force=force, dry_run=dry_run)
    report["distill"] = d
    if d.get("skipped") and not force:
        # 没新 trace 时也做一次策略验证（可能已有策略在等待评分）
        pass
    if d.get("success") or force:
        try:
            report["update"] = update.evaluate_policies()
        except Exception as exc:
            report["update"] = {"success": False, "error": str(exc)}
            logger.error("evolve.update_failed", error=str(exc)[:200])
    return report


def maybe_auto_evolve() -> dict[str, Any]:
    """自动触发检查：间隔足够 + 新 trace 足够 → 后台线程执行。

    同步返回，不阻塞调用方；实际蒸馏在 daemon 线程中运行。
    """
    state = exp.load_state()
    now = datetime.now()

    # 1. 间隔节流
    last = state.get("last_auto_evolve_at", "")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=exp.AUTO_EVOLVE_MIN_INTERVAL_H):
                return {"triggered": False, "reason": "间隔未到"}
        except ValueError:
            pass

    # 2. 新 trace 数量
    traces = exp.load_traces(limit=200)
    undistilled = exp.get_undistilled(traces, state)
    if len(undistilled) < exp.MIN_DISTILL_TRACES:
        return {"triggered": False, "reason": f"新 trace 不足（{len(undistilled)}/{exp.MIN_DISTILL_TRACES}）"}

    # 3. 立即占位（防止并发重复触发），再后台执行
    state["last_auto_evolve_at"] = now.isoformat()
    state["auto_evolve_count"] = int(state.get("auto_evolve_count", 0)) + 1
    exp.save_state(state)

    def _background() -> None:
        try:
            report = evolve_now(force=False)
            logger.info("evolve.auto.done", distill=report.get("distill", {}).get("success"),
                        update=report.get("update", {}).get("evaluated"))
        except Exception as exc:
            logger.error("evolve.auto.failed", error=str(exc)[:300])

    threading.Thread(target=_background, daemon=True, name="agent-evolve").start()
    return {"triggered": True, "undistilled": len(undistilled),
            "note": "蒸馏已在后台启动"}


def status() -> dict[str, Any]:
    """汇总状态（CLI `evolve status` 用）。"""
    return {
        "experience": exp.status_summary(),
        "policies": update.policy_summary(),
        "skills": update.list_skills(),
    }
