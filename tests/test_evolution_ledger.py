"""Ledger（P4 治理层）测试：事件流水 + harness 快照 roundtrip + prune + 回滚。

全部 monkeypatch 隔离，不触碰真实 agent_data。
"""
from __future__ import annotations

import json

import pytest

from app.agent.evolution import experience as exp, ledger, update


@pytest.fixture
def ledger_env(monkeypatch, tmp_path):
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True)
    mem_dir = tmp_path / "memory"
    (mem_dir / "skills").mkdir(parents=True)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)

    monkeypatch.setattr(exp, "_TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    monkeypatch.setattr(update, "_POLICIES_JSON", evo_dir / "policies.json")
    monkeypatch.setattr(update, "_POLICIES_MD", mem_dir / "policies.md")
    monkeypatch.setattr(update, "_SKILLS_DIR", mem_dir / "skills")
    monkeypatch.setattr(ledger, "_LEDGER_PATH", evo_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "_SNAPSHOTS_DIR", evo_dir / "snapshots")
    import app.agent.topic_memory as tm
    monkeypatch.setattr(tm, "_MEMORY_DIR", mem_dir)
    monkeypatch.setattr(tm, "_INDEX_FILE", mem_dir / "MEMORY.md")
    from app.tool_registry import dynamic_tools as dt
    monkeypatch.setattr(dt, "_TOOLS_DIR", tools_dir)
    monkeypatch.setattr(dt, "_PENDING_DIR", tools_dir / "pending")
    # meta 层存在时一并隔离（Phase 1 起）
    try:
        from app.agent.evolution import meta
        monkeypatch.setattr(meta, "_META_CONFIG", evo_dir / "meta_config.json")
        monkeypatch.setattr(meta, "_META_STATS", evo_dir / "meta_stats.json")
        monkeypatch.setattr(meta, "_PROMPTS_DIR", evo_dir / "prompts")
    except ImportError:
        pass
    return {"evo": evo_dir, "mem": mem_dir, "tools": tools_dir}


# ---------------------------------------------------------------------------
# 事件流水
# ---------------------------------------------------------------------------

def test_log_and_read_ledger(ledger_env):
    ledger.log_event("evolve_run", force=False, promoted=1)
    ledger.log_event("distill_run", batch_id="b1")
    ledger.log_event("policy_promoted", policy_id="pol_x")

    rows = ledger.read_ledger()
    assert [r["event"] for r in rows] == ["evolve_run", "distill_run", "policy_promoted"]
    assert all("ts" in r for r in rows)
    assert rows[0]["promoted"] == 1

    # 按事件过滤 + limit
    assert len(ledger.read_ledger(event="policy_promoted")) == 1
    assert len(ledger.read_ledger(limit=2)) == 2
    assert rows[2]["policy_id"] == "pol_x"


def test_read_ledger_missing_file(ledger_env):
    assert ledger.read_ledger() == []


def test_log_event_never_raises(ledger_env, monkeypatch):
    # payload 不可序列化也不打断调用方（default=str 兜底 + 异常收敛）
    ledger.log_event("weird", payload=object())
    ledger.log_event("ok")
    assert len(ledger.read_ledger()) == 2


# ---------------------------------------------------------------------------
# 快照 roundtrip
# ---------------------------------------------------------------------------

def test_snapshot_and_restore_roundtrip(ledger_env):
    policies = [{"policy_id": "pol_a", "task_type": "chatbot",
                 "action": "old action", "status": "active", "score": 3}]
    ledger_env["evo"].joinpath("policies.json").write_text(
        json.dumps(policies, ensure_ascii=False), encoding="utf-8")
    exp.save_state({"distill_count": 7})
    skill = ledger_env["mem"] / "skills" / "my-skill.md"
    skill.write_text("---\nname: my-skill\n---\nbody", encoding="utf-8")
    (ledger_env["tools"] / "get_weather.py").write_text("def handler(**kw): return 'x'", encoding="utf-8")
    (ledger_env["tools"] / "get_weather.json").write_text("{}", encoding="utf-8")

    snap_id = ledger.take_snapshot("pre")
    assert snap_id
    snaps = ledger.list_snapshots()
    assert len(snaps) == 1 and snaps[0]["snap_id"] == snap_id
    assert snaps[0]["files"] == 5  # policies + state + skill + tools .py/.json

    # manifest 哈希存在
    mf = json.loads((ledger._SNAPSHOTS_DIR / snap_id / "manifest.json").read_text(encoding="utf-8"))
    assert set(mf["files"]) >= {"evolution/policies.json", "evolution/state.json",
                                "memory/skills/my-skill.md", "tools/get_weather.py",
                                "tools/get_weather.json"}

    # 破坏现场 → 回滚 → 恢复原状
    policies[0]["action"] = "mutated after snapshot"
    ledger_env["evo"].joinpath("policies.json").write_text(
        json.dumps(policies, ensure_ascii=False), encoding="utf-8")
    skill.unlink()

    r = ledger.restore_snapshot(snap_id)
    assert r["success"] is True
    restored_policies = json.loads(ledger_env["evo"].joinpath("policies.json").read_text(encoding="utf-8"))
    assert restored_policies[0]["action"] == "old action"
    assert skill.exists()

    # 台账记录了 snapshot_taken 与 rollback 两个事件
    events = [row["event"] for row in ledger.read_ledger()]
    assert "snapshot_taken" in events and "rollback" in events


def test_restore_missing_snapshot(ledger_env):
    r = ledger.restore_snapshot("snap_20990101_000000_pre")
    assert r["success"] is False
    assert "不存在" in r["error"]


def test_snapshot_prune(ledger_env, monkeypatch):
    exp.save_state({"distill_count": 1})
    # 同秒内多次快照会撞名：直接操纵 label 区分，或改时间格式 —— 用不同 label
    ids = [ledger.take_snapshot(f"l{i:02d}") for i in range(12)]
    ids = [i for i in ids if i]
    snaps = ledger.list_snapshots()
    assert len(snaps) == ledger.MAX_SNAPSHOTS
    # 最旧的被裁剪：l00 不在了，l11 还在
    snap_names = [s["snap_id"] for s in snaps]
    assert not any("_l00" in n for n in snap_names)
    assert any("_l11" in n for n in snap_names)
