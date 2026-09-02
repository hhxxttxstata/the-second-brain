"""A/B 晋升门（P2）测试：verdict 判定 / 晋升-挂起-退役路径 / spawn 解析 / 用例选择。"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

import pytest

from app.agent.evolution import ab_gate, experience as exp, ledger, meta, update
from app.agent.evolution import eval as evo_eval


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True)
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True)
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir(parents=True)
    monkeypatch.setattr(exp, "_TRACES_DIR", traces_dir)
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    monkeypatch.setattr(update, "_POLICIES_JSON", evo_dir / "policies.json")
    monkeypatch.setattr(update, "_POLICIES_MD", mem_dir / "policies.md")
    monkeypatch.setattr(update, "_SKILLS_DIR", mem_dir / "skills")
    monkeypatch.setattr(meta, "_META_CONFIG", evo_dir / "meta_config.json")
    monkeypatch.setattr(meta, "_META_STATS", evo_dir / "meta_stats.json")
    monkeypatch.setattr(meta, "_PROMPTS_DIR", evo_dir / "prompts")
    monkeypatch.setattr(ledger, "_LEDGER_PATH", evo_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "_SNAPSHOTS_DIR", evo_dir / "snapshots")
    monkeypatch.setattr(ab_gate, "_REPORT_DIR", tmp_path / "benchmark")
    import app.agent.topic_memory as tm
    monkeypatch.setattr(tm, "_MEMORY_DIR", mem_dir)
    monkeypatch.setattr(tm, "_INDEX_FILE", mem_dir / "MEMORY.md")
    return tmp_path


def _policy(score=3, **kw):
    return {
        "policy_id": "pol_ab", "task_type": "chatbot",
        "trigger": "test kw", "triggers": ["test", "kw"],
        "action": "Batch small writes into one call",
        "benefit": "fewer tool calls", "status": "proposed", "score": score,
        "metrics": {}, "created_at": datetime.now().isoformat(),
        "last_seen_at": datetime.now().isoformat(), **kw,
    }


_CASES = [{"id": "c1", "input": "hello", "expected_route": "chatbot"},
          {"id": "c2", "input": "world", "expected_route": "chatbot"},
          {"id": "c3", "input": "other", "expected_route": "memory"}]


def _stub_cases(monkeypatch):
    monkeypatch.setattr(evo_eval, "load_evolution_cases", lambda: _CASES)
    import app.agent.trace as tr
    monkeypatch.setattr(tr, "load_test_cases", lambda tier=None, path=None: [])


def _fake_runner(results_a: list[dict], results_b: list[dict], monkeypatch):
    """FIFO 供结果：先阶段 A（每个 case 一次）再阶段 B。"""
    queue = [dict(r) for r in results_a] + [dict(r) for r in results_b]

    def fake_run(case):
        r = queue.pop(0)
        return {"case_id": case.get("id", "?"), "input": case.get("input", ""),
                "success": r["success"], "route": "chatbot",
                "latency_ms": r.get("latency_ms", 100),
                "tokens": r.get("tokens", 100), "tools": []}

    monkeypatch.setattr(evo_eval, "_run_case", fake_run)


# ---------------------------------------------------------------------------
# run_policy_ab 本体（_run_case 打桩）
# ---------------------------------------------------------------------------

def test_run_policy_ab_pass(gate_env, monkeypatch):
    _stub_cases(monkeypatch)
    _fake_runner(
        [{"success": True, "latency_ms": 100, "tokens": 100},
         {"success": True, "latency_ms": 100, "tokens": 100}],
        [{"success": True, "latency_ms": 80, "tokens": 90},
         {"success": True, "latency_ms": 80, "tokens": 90}],
        monkeypatch)

    r = ab_gate.run_policy_ab(_policy())
    assert r["pass"] is True, r
    assert r["cases_used"] == 2          # route=chatbot 的 2 条
    assert r["latency_delta_pct"] == -20.0
    assert r["report_file"].startswith("policy_ab_pol_ab_")
    # 隔离区内模拟固化确实写了 skill（进的是 tmp，非真实 SKILLS_DIR 恢复后可见隔离效果）
    assert r.get("reason")


def test_run_policy_ab_fail_on_success_drop(gate_env, monkeypatch):
    _stub_cases(monkeypatch)
    _fake_runner(
        [{"success": True}, {"success": True}],
        [{"success": True}, {"success": False}],
        monkeypatch)

    r = ab_gate.run_policy_ab(_policy())
    assert r["pass"] is False
    assert r["success_delta_pct"] < 0


def test_run_policy_ab_unavailable_on_errors(gate_env, monkeypatch):
    _stub_cases(monkeypatch)

    def boom(case):
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(evo_eval, "_run_case", boom)
    r = ab_gate.run_policy_ab(_policy())
    assert r["pass"] is None
    assert "LLM" in r["reason"]


def test_select_cases_falls_back_to_all(gate_env, monkeypatch):
    monkeypatch.setattr(evo_eval, "load_evolution_cases",
                        lambda: _CASES)  # 无 memory route 命中 → 全量
    import app.agent.trace as tr
    monkeypatch.setattr(tr, "load_test_cases", lambda tier=None, path=None: [])
    p = _policy()
    p["task_type"] = "reflect"
    cases = ab_gate.select_cases(p, max_cases=6)
    assert len(cases) == 3


def test_spawn_parses_json(monkeypatch):
    from types import SimpleNamespace

    def fake_run(cmd, **kw):
        assert "_abgate" in cmd and "pol_x" in cmd
        return SimpleNamespace(returncode=0,
                               stdout='日志行\n{"pass": true, "reason": "ok"}',
                               stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = ab_gate.run_policy_ab_spawn("pol_x")
    assert r["pass"] is True


def test_spawn_degrades_on_bad_exit(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="boom"))
    r = ab_gate.run_policy_ab_spawn("pol_x")
    assert r["pass"] is None


# ---------------------------------------------------------------------------
# evaluate_policies 的晋升路径（run_policy_ab 打桩）
# ---------------------------------------------------------------------------

def _write_window_traces(traces_dir):
    """12 条 chatbot trace：后 5 条更快（improved 窗口）。"""
    from datetime import timedelta
    now = datetime.now()
    rows = []
    for i in range(12):
        fast = i >= 7
        rows.append({
            "trace_id": f"trace_{i:02d}", "task_type": "chatbot",
            "timestamp": (now - timedelta(minutes=12 - i)).isoformat(),
            "user_intent": f"intent {i}",
            "latency_ms": 5000 if fast else 9000,
            "total_tokens": 8000 if fast else 12000,
            "success": True, "tool_calls": [],
        })
    for r in rows:
        (traces_dir / f"{r['trace_id']}.json").write_text(
            json.dumps(r, ensure_ascii=False), encoding="utf-8")


def test_promote_passes_gate(gate_env, monkeypatch):
    _write_window_traces(gate_env / "traces")
    update._save_policies([_policy(score=2)])
    monkeypatch.setattr(ab_gate, "run_policy_ab",
                        lambda p, **kw: {"pass": True, "reason": "stub",
                                         "report_file": "r.json"})
    r = update.evaluate_policies()
    assert r["promoted"] == ["pol_ab"]
    p = update._load_policies()[0]
    assert p["status"] == "active"
    assert p["ab"]["verdict"] == "pass"
    # skill 带溯源回链 + 短关键词 triggers
    skill = (gate_env / "memory" / "skills").glob("*.md")
    body = next(skill).read_text(encoding="utf-8")
    assert "policy_id: pol_ab" in body and "triggers: test, kw" in body
    assert any(e["event"] == "policy_ab_gate" for e in ledger.read_ledger())


def test_promote_fail_resets_and_retires_after_two(gate_env, monkeypatch):
    _write_window_traces(gate_env / "traces")
    monkeypatch.setattr(ab_gate, "run_policy_ab",
                        lambda p, **kw: {"pass": False, "reason": "成功率下降",
                                         "report_file": "r.json"})
    update._save_policies([_policy(score=2)])
    r1 = update.evaluate_policies()
    assert r1["promoted"] == [] and r1["retired"] == []
    p = update._load_policies()[0]
    assert p["score"] == 0 and p["ab_fails"] == 1

    # 第二轮：score 重新攒到阈值再 FAIL → 退役
    p["score"] = 3
    update._save_policies([p])
    r2 = update.evaluate_policies()
    assert r2["retired"] == ["pol_ab"]
    assert update._load_policies()[0]["status"] == "retired"


def test_promote_unavailable_marks_pending(gate_env, monkeypatch):
    _write_window_traces(gate_env / "traces")
    monkeypatch.setattr(ab_gate, "run_policy_ab",
                        lambda p, **kw: {"pass": None, "reason": "LLM 不可用"})
    update._save_policies([_policy(score=2)])
    r = update.evaluate_policies()
    assert r["promoted"] == [] and r["pending"] == ["pol_ab"]
    assert update._load_policies()[0]["status"] == "promote_pending"


def test_gate_disabled_promotes_directly(gate_env, monkeypatch):
    _write_window_traces(gate_env / "traces")
    meta.set_params({"policy_ab_gate": False})
    called = {"n": 0}

    def should_not_run(p, **kw):
        called["n"] += 1
        return {"pass": False}

    monkeypatch.setattr(ab_gate, "run_policy_ab", should_not_run)
    update._save_policies([_policy(score=2)])
    r = update.evaluate_policies()
    assert r["promoted"] == ["pol_ab"]
    assert called["n"] == 0
