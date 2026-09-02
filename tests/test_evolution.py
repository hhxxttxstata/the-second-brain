"""Self-Evolution 闭环测试（L1 蒸馏游标 + L2 策略 promote/retire + 注入）。

全部使用 monkeypatch 隔离路径，不触碰真实 agent_data；LLM 用 FakeModel。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.agent.evolution import distill, experience as exp, update


@pytest.fixture
def evo_env(monkeypatch, tmp_path):
    """把 evolution 的 trace/state/policy/meta/ledger 路径全部隔离到 tmp_path。"""
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir(parents=True)
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True)
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True)

    monkeypatch.setattr(exp, "_TRACES_DIR", traces_dir)
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    monkeypatch.setattr(update, "_POLICIES_JSON", evo_dir / "policies.json")
    monkeypatch.setattr(update, "_POLICIES_MD", mem_dir / "policies.md")
    monkeypatch.setattr(update, "_SKILLS_DIR", mem_dir / "skills")
    # meta 层（进化参数/统计/prompt 资产）与治理台账一并隔离
    from app.agent.evolution import ledger, meta
    monkeypatch.setattr(meta, "_META_CONFIG", evo_dir / "meta_config.json")
    monkeypatch.setattr(meta, "_META_STATS", evo_dir / "meta_stats.json")
    monkeypatch.setattr(meta, "_PROMPTS_DIR", evo_dir / "prompts")
    monkeypatch.setattr(ledger, "_LEDGER_PATH", evo_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "_SNAPSHOTS_DIR", evo_dir / "snapshots")
    # topic_memory 也隔离（upsert_index_entry / write_topic 写 MEMORY.md / lessons.md）
    import app.agent.topic_memory as tm
    monkeypatch.setattr(tm, "_MEMORY_DIR", mem_dir)
    monkeypatch.setattr(tm, "_INDEX_FILE", mem_dir / "MEMORY.md")
    return {"traces": traces_dir, "evo": evo_dir, "mem": mem_dir}


def _write_traces(traces_dir: Path, rows: list[dict]) -> None:
    for r in rows:
        (traces_dir / f"{r['trace_id']}.json").write_text(
            json.dumps(r, ensure_ascii=False), encoding="utf-8")


def _chatbot_rows(n: int, fast_from: int = 7) -> list[dict]:
    """n 条 chatbot trace；index >= fast_from 的更快（模拟改进窗口）。"""
    now = datetime.now()
    rows = []
    for i in range(n):
        fast = i >= fast_from
        rows.append({
            "trace_id": f"trace_{i:02d}",
            "task_type": "chatbot",
            "timestamp": (now - timedelta(minutes=n - i)).isoformat(),
            "user_intent": f"intent {i}",
            "latency_ms": 5000 if fast else 9000,
            "total_tokens": 8000 if fast else 12000,
            "success": True,
            "tool_calls": [{"name": "search_vault", "params": {}, "success": True}],
        })
    return rows


class _FakeDistillModel:
    """返回空蒸馏结果（不写经验/策略），只测游标推进。"""

    class _Resp:
        content = '{"experiences": [], "policy_suggestions": []}'

    def invoke(self, prompt):
        return self._Resp()


class _FakePromoteModel:
    """返回 1 条策略建议（测 apply_policy_suggestions）。"""

    class _Resp:
        content = ('{"experiences": [], "policy_suggestions": ['
                   '{"task_type": "chatbot", "trigger": "test kw", '
                   '"action": "Directly call search_vault first", "benefit": "faster"}]}')

    def invoke(self, prompt):
        return self._Resp()


# ---------------------------------------------------------------------------
# L1: 蒸馏游标分批推进
# ---------------------------------------------------------------------------

def test_distill_batches_without_skip_or_duplicate(evo_env, monkeypatch):
    monkeypatch.setattr(distill, "get_chat_model", lambda **kw: _FakeDistillModel())
    _write_traces(evo_env["traces"], _chatbot_rows(30))

    r1 = distill.distill_once()
    assert r1["success"] and r1["batch_size"] == 12
    r2 = distill.distill_once()
    assert r2["success"] and r2["batch_size"] == 12
    r3 = distill.distill_once()
    assert r3["success"] and r3["batch_size"] == 6   # 剩余 6 条
    r4 = distill.distill_once()
    assert r4["skipped"]                              # 无剩余 → 跳过

    state = exp.load_state()
    assert state["distill_count"] == 3
    # 游标推进到最新一条 trace 的时间戳（30 条全部消费）
    assert state["last_distilled_at"].startswith(
        (datetime.now() - timedelta(minutes=1)).isoformat()[:16])


def test_distill_skips_benchmark_traces(evo_env, monkeypatch):
    monkeypatch.setattr(distill, "get_chat_model", lambda **kw: _FakeDistillModel())
    rows = _chatbot_rows(3)
    rows.append({"trace_id": "trace_b", "task_type": "benchmark",
                 "timestamp": datetime.now().isoformat(), "success": True})
    _write_traces(evo_env["traces"], rows)

    u = exp.get_undistilled(exp.load_traces(100), {})
    assert len(u) == 3                       # benchmark 被排除
    assert all(t["task_type"] != "benchmark" for t in u)
    # 3 条普通 < 阈值 5 → skipped
    r = distill.distill_once()
    assert r["skipped"]


def test_compare_windows_ignores_metricless_traces(evo_env):
    """无度量指标的旧式 trace 不进窗口对比（count 只数带指标者）。"""
    rows = _chatbot_rows(12)
    for r in rows[5:]:               # 7 条退化为旧式无指标 trace（无 latency/tokens/tool_calls）
        r["latency_ms"] = 0
        r["total_tokens"] = 0
        r["tool_calls"] = []
    _write_traces(evo_env["traces"], rows)

    traces = exp.load_traces(100)
    cmp = exp.compare_windows(traces, "chatbot")
    assert cmp["verdict"] == "insufficient"   # 带指标的仅 5 条 < MIN_SAMPLES
    assert cmp["count"] == 5


# ---------------------------------------------------------------------------
# L2: 策略应用 → 验证 → skill 固化 → 回滚
# ---------------------------------------------------------------------------

def test_policy_suggestion_applied(evo_env, monkeypatch):
    monkeypatch.setattr(distill, "get_chat_model", lambda **kw: _FakePromoteModel())
    _write_traces(evo_env["traces"], _chatbot_rows(12))

    r = distill.distill_once()
    assert r["policy_suggestions"] == 1
    assert r["policy_result"]["added"] == 1

    policies = update._load_policies()
    assert policies[0]["status"] == "proposed"
    assert policies[0]["task_type"] == "chatbot"
    # policies.md 同步生成（注入源）
    assert (evo_env["mem"] / "policies.md").exists()


def test_promote_to_skill_after_three_improvements(evo_env, monkeypatch):
    _write_traces(evo_env["traces"], _chatbot_rows(12))
    policies = [{
        "policy_id": "pol_test", "task_type": "chatbot",
        "trigger": "test kw", "action": "Directly call search_vault first",
        "benefit": "faster", "status": "proposed", "score": 2,
        "metrics": {}, "created_at": datetime.now().isoformat(),
        "last_seen_at": datetime.now().isoformat(),
    }]
    update._save_policies(policies)

    # A/B 晋升门打桩通过（真实门行为由 test_policy_ab_gate.py 覆盖）
    from app.agent.evolution import ab_gate
    monkeypatch.setattr(ab_gate, "run_policy_ab",
                        lambda p, **kw: {"pass": True, "reason": "stub",
                                         "report_file": "stub.json"})

    r = update.evaluate_policies()
    assert r["promoted"] == ["pol_test"]
    assert update._load_policies()[0]["status"] == "active"

    skill_files = list((evo_env["mem"] / "skills").glob("*.md"))
    assert len(skill_files) == 1
    idx = (evo_env["mem"] / "MEMORY.md")
    # upsert_index_entry 在无 MEMORY.md 时自动生成
    assert idx.exists() and "## Skills" in idx.read_text(encoding="utf-8")


def test_retire_after_regression(evo_env, monkeypatch):
    # 数据：最近 5 次变慢且更贵（latency/tokens 双 regressed）
    rows = _chatbot_rows(12)
    for r in rows:
        if r["latency_ms"] == 5000:      # 原 fast 窗口 → 反转
            r["latency_ms"] = 12000
            r["total_tokens"] = 15000
    _write_traces(evo_env["traces"], rows)
    policies = [{
        "policy_id": "pol_test", "task_type": "chatbot",
        "trigger": "test kw", "action": "Directly call search_vault first",
        "benefit": "faster", "status": "proposed", "score": -1,
        "metrics": {}, "created_at": datetime.now().isoformat(),
        "last_seen_at": datetime.now().isoformat(),
    }]
    update._save_policies(policies)

    r = update.evaluate_policies()
    assert r["retired"] == ["pol_test"]
    assert update._load_policies()[0]["status"] == "retired"


# ---------------------------------------------------------------------------
# L2: 上下文注入
# ---------------------------------------------------------------------------

def test_build_evolution_block_injects_policy_and_skill(evo_env):
    _write_traces(evo_env["traces"], _chatbot_rows(12))
    update._save_policies([{
        "policy_id": "pol_x", "task_type": "chatbot", "trigger": "test kw",
        "action": "Directly call search_vault first", "benefit": "faster",
        "status": "proposed", "score": 0, "metrics": {},
        "created_at": datetime.now().isoformat(),
    }])
    update._promote_to_skill(update._load_policies()[0])

    block = update.build_evolution_block(task="test kw question")
    assert "执行策略" in block
    assert "Skill:" in block

    # 未命中 trigger 时不注入 skill 详情
    block2 = update.build_evolution_block(task="unrelated question")
    assert "Skill:" not in block2
