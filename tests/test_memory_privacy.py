# -*- coding: utf-8 -*-
"""隐私记忆 + git ignore 隔离回归测试（bad case: candidate-captured-20260826160624）。

bad case 原行为：用户要求"存入记忆系统，但写入git ignore"，
memory agent 判定"无需重复记忆"直接拒绝（声称 vault 读写已开放、无需 git ignore）。

修复后行为：
  1. 显式记忆指令（存入记忆/记住/记一下...）→ 尊重用户意图，LLM 误判 skip 也强制 write
  2. 隐私隔离诉求（git ignore/隐私/隔离...）→ 落实/确认 .gitignore 对 agent_data 的忽略
  3. 普通闲聊仍正常 skip（不因修复而过度写入）

FakeModel（conftest）对含 "memory-worthiness" 的 prompt 恒返回 decision=skip，
正好用于验证"LLM 判 skip 时显式指令仍强制写入"。
"""
from __future__ import annotations

import pytest

from app.agent.graphs import memory_graph


@pytest.fixture
def tmp_gitignore(monkeypatch, tmp_path):
    """把 .gitignore 重定向到临时目录，避免污染真实仓库文件。"""
    gi = tmp_path / ".gitignore"
    monkeypatch.setattr(memory_graph, "_GITIGNORE_PATH", gi)
    return gi


def _run(trigger: str) -> dict:
    return memory_graph.run_memory_agent(trigger_text=trigger)


def test_explicit_privacy_command_forces_write_and_isolation(
        mock_llm, isolated_data, tmp_gitignore):
    """bad case 主路径：LLM 判 skip 时，显式'存入记忆系统'仍写入 + 落实 git ignore 隔离。"""
    tmp_gitignore.write_text("__pycache__/\n", encoding="utf-8")  # 未忽略 agent_data
    r = _run("存入记忆系统，但写入git ignore")

    assert r["success"] is True
    assert r["decision"] == "write"
    # 输出包含隐私隔离说明（确认或已配置）
    assert "隐私隔离" in r["summary"] or "git" in r["summary"].lower()
    # 未忽略 → 已追加 agent_data 隔离条目
    content = tmp_gitignore.read_text(encoding="utf-8")
    assert "agent_data/*" in content
    assert "!agent_data/eval/" in content
    # 不出现"无需记忆"式拒绝
    assert "无需重复记忆" not in r["summary"]


def test_gitignore_already_isolated_confirms_without_duplication(
        mock_llm, isolated_data, tmp_gitignore):
    """已隔离：只确认不重复写入。"""
    tmp_gitignore.write_text("# 已忽略\nagent_data/*\n!agent_data/eval/\n", encoding="utf-8")
    r = _run("存入记忆系统，但写入git ignore")

    assert r["decision"] == "write"
    assert "已确认" in r["summary"]
    content = tmp_gitignore.read_text(encoding="utf-8")
    assert content.count("agent_data/*") == 1


def test_gitignore_isolation_idempotent(mock_llm, isolated_data, tmp_gitignore):
    """连续两次同样请求：不重复追加 .gitignore 条目。"""
    tmp_gitignore.write_text("__pycache__/\n", encoding="utf-8")
    _run("记住我的隐私，写入git ignore")
    _run("记住我的隐私，写入git ignore")
    content = tmp_gitignore.read_text(encoding="utf-8")
    assert content.count("agent_data/*") == 1


def test_explicit_memory_command_forced_write_when_llm_skips(
        mock_llm, isolated_data, tmp_gitignore):
    """显式'记住'指令：FakeModel 判 skip 仍强制写入，且内容为确定性兜底而非 LLM 垃圾。"""
    tmp_gitignore.write_text("agent_data/*\n", encoding="utf-8")
    r = _run("记住：以后代码示例默认用 Python")

    assert r["decision"] == "write"
    assert "已" in r["summary"]  # 保存确认（已保存/已确认/已更新）
    # 记忆确实落库（内容不是 FakeModel 的 '测试'）
    from app.agent.agent_data_service import read_memory
    episodic = read_memory("episodic")
    contents = " ".join(str(e.get("content", "")) for e in episodic.get("entries", []))
    assert "Python" in contents


def test_chat_noise_still_skips(mock_llm, isolated_data, tmp_gitignore):
    """普通闲聊不受影响：无显式指令时 LLM 判 skip 仍 skip（防止过度写入）。"""
    tmp_gitignore.write_text("agent_data/*\n", encoding="utf-8")
    r = _run("今天天气真不错")
    assert r["decision"] == "skip"
    assert "无需" in r["summary"] or "(skipped)" in r["summary"]


def test_gitignore_path_points_to_repo_root(mock_llm, isolated_data):
    """路径守卫：_GITIGNORE_PATH 必须指向项目根（agent_data 的父目录），防止误写 app/.gitignore。"""
    import pathlib
    gi = pathlib.Path(memory_graph._GITIGNORE_PATH)
    # 与 settings.agent_data_dir 同级（agent_data 的父目录 = 项目根）
    from app.core.config import settings
    assert gi == settings.agent_data_dir.parent / ".gitignore"
    assert (settings.agent_data_dir.parent / "agent_data").exists()


def test_privacy_without_explicit_cmd_still_confirms_isolation(
        mock_llm, isolated_data, tmp_gitignore):
    """隐私隔离诉求即使无显式记忆指令也落实（skip 分支也带隔离说明）。"""
    tmp_gitignore.write_text("__pycache__/\n", encoding="utf-8")
    r = _run("把隐私内容隔离，不要提交到 git")
    assert "隐私隔离" in r["summary"] or "git" in r["summary"].lower()
    assert "agent_data/*" in tmp_gitignore.read_text(encoding="utf-8")
