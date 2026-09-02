"""Pytest fixtures — checkpoint 改造回归测试的公共设施。

隔离策略:
  - checkpoint.db / memory.db 重定向到 pytest tmp_path
  - LLM 全部 mock (FakeModel 按输入内容返回对应响应), 不发真实 API 请求
  - 重置所有图的模块级 _graph 缓存 (它们持有旧 checkpointer 实例)

注: plan 路由测试会写入真实的 agent_data/state/{date}.json (commit_node
调用 save_today_state), 属于可接受的轻量污染, 不影响断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest
from langchain_core.messages import AIMessage


class FakeModel:
    """按输入内容路由响应的假 LLM; 记录每次 invoke 的输入供断言。

    - route: 设置后 planner prompt 返回 {"route": self.route}（旧单路由格式）
    - tasks: 设置后 planner prompt 返回 {"tasks": self.tasks}（多任务格式）
    """

    def __init__(self) -> None:
        self.calls: list = []
        self.route: str = "chatbot"
        self.tasks: list | None = None

    def bind_tools(self, tools):
        return self

    @staticmethod
    def _text(input_) -> str:
        if isinstance(input_, str):
            return input_
        parts = []
        for m in input_:
            c = getattr(m, "content", "")
            if isinstance(c, list):  # content blocks
                c = " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
            parts.append(str(c))
        return "\n".join(parts)

    def invoke(self, input_):
        self.calls.append(input_)
        text = self._text(input_)
        if "route it to the correct sub-agent" in text:
            if self.tasks is not None:
                return AIMessage(content=f'{{"tasks": {json.dumps(self.tasks, ensure_ascii=False)}, "reason": "test-multi"}}')
            return AIMessage(content=f'{{"route": "{self.route}", "reason": "test-route"}}')
        if "You are a daily planning assistant" in text:
            return AIMessage(content='[{"title": "测试计划项", "priority": "medium", "source": "test"}]')
        if "Evaluate this plan" in text:
            return AIMessage(content="ok")
        if "todo/task manager" in text:
            return AIMessage(content='{"ops": [], "summary": "无变更"}')
        if "memory-worthiness" in text:
            return AIMessage(content='{"decision": "skip", "reason": "test", "type": "episodic", "content_memory": "测试"}')
        if "Suggestions based on" in text:
            return AIMessage(content='{"summary": "测试总结", "action_items": ["动作1"]}')
        if "Extract key facts" in text:
            return AIMessage(content='{"summary": "测试摘要：关键事实与决策参数12345", "stable_insights": ["用户偏好先给结论再给细节"], "temporary_state": ["用户今天比较忙"]}')
        if "Analyze this content" in text:
            return AIMessage(content="测试分析")
        if "Constructive critique" in text:
            return AIMessage(content="测试批判")
        return AIMessage(content="测试回答")


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def mock_llm(monkeypatch, fake_model: FakeModel) -> FakeModel:
    """把所有 graph 模块 + llm 工厂 + evolution.distill 的 get_chat_model 替换为 FakeModel。"""
    import app.agent.graphs.orchestrator as orch
    import app.agent.graphs.chatbot_graph as cbg
    import app.agent.graphs.plan_graph as pg
    import app.agent.graphs.reflect_graph as rg
    import app.agent.graphs.memory_graph as mg
    import app.agent.graphs.llm as llm_mod
    import app.agent.evolution.distill as ed
    import app.agent.evolution.meta as em

    for mod in (orch, cbg, pg, rg, mg, ed, em):
        monkeypatch.setattr(mod, "get_chat_model", lambda *a, **k: fake_model)
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda *a, **k: fake_model)
    return fake_model


@pytest.fixture
def isolated_data(monkeypatch, tmp_path):
    """隔离 checkpoint + memory.db + traces + evolution 到临时目录, 并重置图缓存。"""
    import app.agent.checkpoint as cp
    import app.agent.memory_store as ms
    import app.agent.trace as tr
    import app.agent.graphs.orchestrator as orch
    import app.agent.graphs.chatbot_graph as cbg
    import app.agent.graphs.plan_graph as pg
    import app.agent.graphs.reflect_graph as rg
    import app.agent.graphs.memory_graph as mg

    monkeypatch.setattr(cp, "_checkpoint_path", tmp_path / "checkpoint.db")
    monkeypatch.setattr(cp, "_saver", None)
    monkeypatch.setattr(ms, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(tr, "_TRACE_DIR", tmp_path / "traces")  # 防测试污染真实 traces/
    # evolution 全套路径：orchestrator 每次请求后会触发 maybe_auto_evolve，
    # 若不隔离，真实 traces 的未蒸馏积压会让后台线程写真实进化状态/台账
    from app.agent.evolution import experience as evo_exp, ledger as evo_led, meta as evo_meta, update as evo_upd
    monkeypatch.setattr(evo_exp, "_TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(evo_exp, "_STATE_DIR", tmp_path / "evolution")
    monkeypatch.setattr(evo_exp, "_STATE_PATH", tmp_path / "evolution" / "state.json")
    monkeypatch.setattr(evo_upd, "_POLICIES_JSON", tmp_path / "evolution" / "policies.json")
    monkeypatch.setattr(evo_upd, "_POLICIES_MD", tmp_path / "memory" / "policies.md")
    monkeypatch.setattr(evo_upd, "_SKILLS_DIR", tmp_path / "memory" / "skills")
    monkeypatch.setattr(evo_meta, "_META_CONFIG", tmp_path / "evolution" / "meta_config.json")
    monkeypatch.setattr(evo_meta, "_META_STATS", tmp_path / "evolution" / "meta_stats.json")
    monkeypatch.setattr(evo_meta, "_PROMPTS_DIR", tmp_path / "evolution" / "prompts")
    monkeypatch.setattr(evo_led, "_LEDGER_PATH", tmp_path / "evolution" / "ledger.jsonl")
    monkeypatch.setattr(evo_led, "_SNAPSHOTS_DIR", tmp_path / "evolution" / "snapshots")
    ms._local.conn = None  # 丢弃已缓存到真实库的线程连接
    ms.init_db()

    for mod in (orch, cbg, pg, rg, mg):
        mod._graph = None
    yield
    for mod in (orch, cbg, pg, rg, mg):
        mod._graph = None
