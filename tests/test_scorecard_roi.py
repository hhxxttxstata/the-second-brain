# -*- coding: utf-8 -*-
"""自进化 ROI 维度（P5）测试：collect_roi 指标聚合 + 评分卡维度打分/无数据跳过。"""
from __future__ import annotations

import json

import pytest

from app.agent.evolution import experience as exp, ledger, meta
from app.agent.evolution import roi as roi_mod
from app.agent.evolution.roi import collect_roi, score_evolution_roi


@pytest.fixture
def roi_env(monkeypatch, tmp_path):
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True)
    bench_dir = tmp_path / "benchmark"
    bench_dir.mkdir(parents=True)
    monkeypatch.setattr(exp, "_TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    monkeypatch.setattr(ledger, "_LEDGER_PATH", evo_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "_SNAPSHOTS_DIR", evo_dir / "snapshots")
    monkeypatch.setattr(meta, "_META_CONFIG", evo_dir / "meta_config.json")
    monkeypatch.setattr(meta, "_META_STATS", evo_dir / "meta_stats.json")
    monkeypatch.setattr(meta, "_PROMPTS_DIR", evo_dir / "prompts")
    monkeypatch.setattr(roi_mod, "_REPORT_DIR", bench_dir)
    return {"evo": evo_dir, "bench": bench_dir}


def _seed_stats(runs: int, parse_fail: int):
    meta._save_stats({"versions": {"v1": {
        "runs": runs, "parse_fail": parse_fail,
        "experiences": runs * 2, "suggestions": runs, "tool_requests": 0,
        "first_at": "", "last_at": "",
    }}, "history": []})


def _seed_report(bench_dir, delta: float):
    (bench_dir / "evolution_2099_12_31.json").write_text(
        json.dumps({"latency_delta_pct": delta}), encoding="utf-8")


def test_no_data_skips_dimension(roi_env):
    assert score_evolution_roi()["score"] is None


def test_collect_roi_aggregates_events(roi_env):
    ledger.log_event("policy_promoted", policy_id="a")
    ledger.log_event("policy_promoted", policy_id="b")
    ledger.log_event("policy_ab_gate", policy_id="a", verdict="pass")
    ledger.log_event("distill_run", ok=True)
    _seed_stats(10, 1)
    _seed_report(roi_env["bench"], -10.0)

    roi = collect_roi()
    assert roi["promoted"] == 2 and roi["retired"] == 0
    assert roi["ab_pass"] == 1 and roi["ab_fail"] == 0
    assert roi["distill_runs"] == 10 and roi["parse_fail"] == 1
    assert roi["latency_delta_avg"] == -10.0
    assert roi["prompt_version"] == "v1"


def test_healthy_evolution_scores_high(roi_env):
    ledger.log_event("policy_ab_gate", verdict="pass")
    ledger.log_event("policy_ab_gate", verdict="pass")
    _seed_stats(10, 1)
    _seed_report(roi_env["bench"], -10.0)

    r = score_evolution_roi()
    # 35*1.0 + 25*0.9 + 25*1.0 + 15*0.5（无固化无退役 → 中性） = 90.0
    assert r["score"] >= 85
    assert "A/B 通过率 2/2" in r["detail"]
    assert "演化 latency Δ -10.0%" in r["detail"]


def test_regressing_evolution_scores_low(roi_env):
    ledger.log_event("policy_ab_gate", verdict="fail")
    ledger.log_event("policy_ab_gate", verdict="fail")
    ledger.log_event("policy_retired", policy_id="a")
    ledger.log_event("policy_retired", policy_id="b")
    _seed_stats(10, 5)
    _seed_report(roi_env["bench"], 10.0)

    r = score_evolution_roi()
    assert r["score"] <= 25
    assert "固化 0 / 退役 2" in r["detail"]


def test_scorecard_registers_dimension():
    from app.agent.scorecard import LEVEL_LABELS, LEVEL_PARENTS, WEIGHTS_V2, _score_evolution_roi
    assert "score_evolution_roi" in WEIGHTS_V2
    assert WEIGHTS_V2["score_evolution_roi"] == 0.03
    assert "score_evolution_roi" in LEVEL_LABELS
    assert "score_evolution_roi" in LEVEL_PARENTS
    # 包装函数可用（读的是真实 agent_data，不打断、不抛错即可）
    assert "score" in _score_evolution_roi()
