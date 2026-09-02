"""Meta 元进化层（P1）测试：参数 clamp / prompt 资产 / 蒸馏统计 / 自动回退 / 复审闭环。"""
from __future__ import annotations

import json

import pytest

from app.agent.evolution import experience as exp, ledger, meta


@pytest.fixture
def meta_env(monkeypatch, tmp_path):
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True)
    monkeypatch.setattr(exp, "_TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    monkeypatch.setattr(meta, "_META_CONFIG", evo_dir / "meta_config.json")
    monkeypatch.setattr(meta, "_META_STATS", evo_dir / "meta_stats.json")
    monkeypatch.setattr(meta, "_PROMPTS_DIR", evo_dir / "prompts")
    monkeypatch.setattr(ledger, "_LEDGER_PATH", evo_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "_SNAPSHOTS_DIR", evo_dir / "snapshots")
    return {"evo": evo_dir, "prompts": evo_dir / "prompts"}


class _FakeMetaModel:
    """返回预置的 meta 复审建议 JSON。"""

    def __init__(self, advice: dict):
        self.advice = advice
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        resp = type("R", (), {})()
        resp.content = "```json\n" + json.dumps(self.advice) + "\n```"
        return resp


_VALID_PROMPT = ("You are the reflection engine. Input:\n{traces}\n"
                 "Output JSON with experiences / policy_suggestions / tool_requests.\n" * 5
                 + "Rules: keep limits.\n" * 10)


# ---------------------------------------------------------------------------
# 参数：默认值 / clamp / 白名单
# ---------------------------------------------------------------------------

def test_get_param_defaults_without_config(meta_env):
    assert meta.get_param("max_distill_input") == 12
    assert meta.get_param("promote_threshold") == 3
    assert meta.get_param("policy_ab_gate") is True
    assert meta.get_param("no_such_param") is None


def test_param_clamp_on_load_and_set(meta_env):
    meta._META_CONFIG.write_text(json.dumps({
        "revision": 1, "params": {"max_distill_input": 99, "promote_threshold": 0},
    }), encoding="utf-8")
    assert meta.get_param("max_distill_input") == 16   # 越上界 → clamp
    assert meta.get_param("promote_threshold") == 2    # 越下界 → clamp

    r = meta.set_params({"min_distill_traces": 1, "retire_threshold": -99})
    assert r["applied"] == {"min_distill_traces": 3, "retire_threshold": -4}
    assert meta.get_param("min_distill_traces") == 3


def test_set_params_whitelist_rejects_unknown(meta_env):
    r = meta.set_params({"evil_param": "x", "max_policies": 1000})
    assert r["applied"] == {"max_policies": 20}  # 未知参数被拒，已知 clamp
    # 台账记录了变更
    assert any(e["event"] == "meta_update" for e in ledger.read_ledger())


def test_bool_param_toggle(meta_env):
    meta.set_params({"policy_ab_gate": False})
    assert meta.get_param("policy_ab_gate") is False


# ---------------------------------------------------------------------------
# prompt 资产：seed / 版本切换 / 健全性校验
# ---------------------------------------------------------------------------

def test_load_distill_prompt_seeds_v1(meta_env):
    text, version = meta.load_distill_prompt()
    assert version == "v1"
    assert "{traces}" in text and "policy_suggestions" in text
    assert (meta_env["prompts"] / "distill_v1.txt").exists()
    # 二次加载同一文件
    text2, _ = meta.load_distill_prompt()
    assert text2 == text


def test_prompt_revision_install_and_load(meta_env):
    r = meta._install_prompt_revision(_VALID_PROMPT, rationale="更聚焦")
    assert r["installed"] is True and r["version"] == "v2" and r["previous"] == "v1"

    text, version = meta.load_distill_prompt()
    assert version == "v2" and text == _VALID_PROMPT
    cfg = json.loads(meta._META_CONFIG.read_text(encoding="utf-8"))
    assert cfg["prompt"]["previous"] == "v1"


def test_prompt_revision_rejects_broken(meta_env):
    assert meta._install_prompt_revision("too short")["installed"] is False
    assert meta._install_prompt_revision("no placeholder " * 30)["installed"] is False


# ---------------------------------------------------------------------------
# 蒸馏统计 + prompt 自动回退 / 接受
# ---------------------------------------------------------------------------

def test_prompt_auto_rollback_on_bad_stats(meta_env):
    meta._install_prompt_revision(_VALID_PROMPT)  # v2 试行
    meta.record_distill_outcome("v1", ok=True, experiences=3, suggestions=1)  # 基线好

    for _ in range(3):  # v2 连续 parse 失败
        meta.record_distill_outcome("v2", ok=False)

    # 自动回退：活跃版本回到 v1
    _, version = meta.load_distill_prompt()
    assert version == "v1"
    events = [e["event"] for e in ledger.read_ledger()]
    assert "meta_prompt_rollback" in events


def test_prompt_accepted_when_healthy(meta_env):
    meta._install_prompt_revision(_VALID_PROMPT)
    for _ in range(3):
        meta.record_distill_outcome("v2", ok=True, experiences=2, suggestions=1)
    _, version = meta.load_distill_prompt()
    assert version == "v2"  # 健康则保留


# ---------------------------------------------------------------------------
# meta 复审：节流 / 建议应用
# ---------------------------------------------------------------------------

def test_meta_review_throttle_and_apply(meta_env, monkeypatch):
    exp.save_state({"distill_count": 5})
    fake = _FakeMetaModel({"param_changes": [
        {"name": "max_distill_input", "value": 8, "reason": "批更小更聚焦"},
        {"name": "unknown_knob", "value": 1, "reason": "不在白名单"},
    ], "prompt_revision": None})
    monkeypatch.setattr(meta, "get_chat_model", lambda **kw: fake)

    r = meta.meta_review()
    assert r["success"] and not r.get("skipped")
    assert r["applied"] == {"max_distill_input": 8}
    assert meta.get_param("max_distill_input") == 8
    # 建议信号里带了失败分布与统计
    assert "failure" in fake.prompts[0] or "failures" in fake.prompts[0]

    # 节流：蒸馏批数不足 → 跳过
    r2 = meta.meta_review()
    assert r2.get("skipped") is True


def test_meta_review_applies_prompt_revision(meta_env, monkeypatch):
    exp.save_state({"distill_count": 5})
    fake = _FakeMetaModel({"param_changes": [], "prompt_revision": {
        "new_text": _VALID_PROMPT, "rationale": "parse 失败率偏高"}})
    monkeypatch.setattr(meta, "get_chat_model", lambda **kw: fake)

    r = meta.meta_review()
    assert r["prompt"]["installed"] is True
    _, version = meta.load_distill_prompt()
    assert version == "v2"


def test_meta_review_llm_failure_degrades(meta_env, monkeypatch):
    exp.save_state({"distill_count": 5})

    class _Boom:
        def invoke(self, prompt):
            raise RuntimeError("no key")

    monkeypatch.setattr(meta, "get_chat_model", lambda **kw: _Boom())
    r = meta.meta_review()
    assert r["success"] is False and "LLM" in r["error"]
