"""L3 动态工具自进化测试（create_tool / 持久化 / 启动加载 / 缺工具信号）。

隔离策略（handoff §5-2 教训：凡是要被写的东西，路径全量 monkeypatch）:
  - agent_data/tools/ 与 pending/ 重定向到 tmp_path
  - 全局 registry 替换为测试实例（graphs.tools._registry + 缓存重置）
  - LLM 用 FakeModel，不发真实请求
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tool_registry import dynamic_tools as dt
from app.tool_registry.registry import RegisteredTool, ToolRegistry

GOOD_CODE = (
    "def handler(**kwargs) -> str:\n"
    "    import json\n"
    "    return 'ok:' + str(kwargs.get('x', 0))\n"
)

GOOD_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer", "description": "测试参数"}},
    "required": ["x"],
}


@pytest.fixture
def tool_env(monkeypatch, tmp_path):
    """隔离动态工具目录 + 全局 registry + LLM 工具缓存。"""
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(dt, "_BASE_DIR", tools_dir)
    monkeypatch.setattr(dt, "_TOOLS_DIR", tools_dir)
    monkeypatch.setattr(dt, "_PENDING_DIR", tools_dir / "pending")

    # 全局 registry 换成测试实例（_resolve_registry 走 get_registry 单例）
    import app.agent.graphs.tools as gt

    test_registry = ToolRegistry()
    monkeypatch.setattr(gt, "_registry", test_registry)
    monkeypatch.setattr(gt, "_agent_tools_cache", None)

    # 哨兵原生工具：验证重名/覆盖保护
    test_registry.register_native(RegisteredTool(
        name="search_vault", description="哨兵", schema_={"type": "object", "properties": {}},
        source="native", server_name=None, risk_level="low", side_effects=[],
        handler=lambda **kw: "sentinel",
    ))
    return {"registry": test_registry, "tools_dir": tools_dir,
            "pending_dir": tools_dir / "pending", "graphs_tools": gt}


def _create(registry: ToolRegistry, name: str = "calc_double", **kw) -> dict:
    params = dict(name=name, description="测试用计算工具，翻倍输入",
                  input_schema=GOOD_SCHEMA, code=GOOD_CODE)
    params.update(kw)
    return dt.create_dynamic_tool(registry, **params)


# ---------------------------------------------------------------------------
# 1. 创建 → 注册 → 可见 → 可执行 → 持久化
# ---------------------------------------------------------------------------

def test_create_registers_and_persists(tool_env):
    r = _create(tool_env["registry"])
    assert r["persisted"] and r["risk_level"] == "high"

    reg = tool_env["registry"]
    tool = reg.get_native_tool("calc_double")
    assert tool is not None and tool.source == "dynamic"
    assert reg.version >= 2  # 哨兵 + 动态工具，版本递增
    assert any(t["name"] == "calc_double" for t in reg.list_tools_for_llm())
    # LLM 工具列表缓存已被重置（下一轮请求重建图后立即可见）
    assert tool_env["graphs_tools"]._agent_tools_cache is None

    # 持久化: .py + .json 成对落盘
    assert (tool_env["tools_dir"] / "calc_double.py").exists()
    meta = json.loads((tool_env["tools_dir"] / "calc_double.json").read_text(encoding="utf-8"))
    assert meta["name"] == "calc_double" and meta["risk_level"] == "high"

    # 执行: 真实跑通（含 import 白名单模块）
    import asyncio
    result = asyncio.run(reg.execute("calc_double", {"x": 21}))
    assert result["success"] and result["result"] == "ok:21"


def test_create_with_test_params_runs_smoke(tool_env):
    r = _create(tool_env["registry"], test_params={"x": 2})
    assert r["smoke_tested"] is True
    meta = json.loads(
        (tool_env["tools_dir"] / "calc_double.json").read_text(encoding="utf-8"))
    assert meta["smoke_tested"] is True


# ---------------------------------------------------------------------------
# 2. 非法代码全部被拒（handoff DoD-1）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_code,fragment", [
    ("def handler(**kw):\n    import os\n    return os.getcwd()\n", "白名单"),
    ("def handler(**kw):\n    open('/etc/passwd')\n    return 'x'\n", "open"),
    ("def handler(**kw):\n    print('hi')\n    return 'x'\n", "print"),
    ("def handler(**kw):\n    return eval('1+1')\n", "eval"),
    ("def handler(**kw):\n    return kw.__class__\n", "双下划线"),
    ("def handler(**kw):\n    return __import__('os').getcwd()\n", "__import__"),
    ("def handler(中文):\n    return 'x'\n", "**kwargs"),   # 中文是合法标识符 → 签名错误
    ("def broken(:\n    return 1\n", "语法"),
    ("def other(**kw):\n    return 'x'\n", "handler"),     # 定义了函数但名字不是 handler
    ("async def handler(**kw):\n    return 'x'\n", "async"),
    ("def handler(a, b):\n    return 'x'\n", "**kwargs"),
])
def test_invalid_code_rejected(tool_env, bad_code, fragment):
    with pytest.raises(dt.DynamicToolError) as ei:
        _create(tool_env["registry"], code=bad_code)
    assert fragment in str(ei.value)
    # 零副作用: 未注册、未落盘
    assert tool_env["registry"].get_native_tool("calc_double") is None
    assert not (tool_env["tools_dir"] / "calc_double.py").exists()


def test_function_body_auto_wrapped(tool_env):
    _create(tool_env["registry"],
            code="import math\nreturn math.floor(3.7)\n")
    import asyncio
    result = asyncio.run(tool_env["registry"].execute("calc_double", {"x": 1}))
    assert result["success"] and result["result"] == "3"


def test_full_function_with_import_kept_toplevel(tool_env):
    """LLM 典型输出: import + def handler 组合，函数必须保持顶层（不被当函数体嵌套）。"""
    code = ("import json\n"
            "def handler(**kwargs):\n"
            "    data = json.dumps({'x': kwargs.get('x')})\n"
            "    return data\n")
    _create(tool_env["registry"], code=code)
    import asyncio
    result = asyncio.run(tool_env["registry"].execute("calc_double", {"x": 7}))
    assert result["success"] and '"x": 7' in str(result["result"])


# ---------------------------------------------------------------------------
# 3. 名称与 schema 校验
# ---------------------------------------------------------------------------

def test_name_validation(tool_env):
    for bad in ("Calc-Double", "ab", "1abc", "create_tool", "search_vault"):
        with pytest.raises(dt.DynamicToolError):
            _create(tool_env["registry"], name=bad)
    assert not (tool_env["tools_dir"] / "create_tool.py").exists()


def test_schema_validation(tool_env):
    too_many = {"type": "object", "properties": {
        f"p{i}": {"type": "string"} for i in range(6)}, "required": []}
    with pytest.raises(dt.DynamicToolError, match="参数过多"):
        _create(tool_env["registry"], input_schema=too_many)
    bad_required = {"type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["ghost"]}
    with pytest.raises(dt.DynamicToolError, match="required"):
        _create(tool_env["registry"], input_schema=bad_required)
    bad_type = {"type": "object",
                "properties": {"a": {"type": "websocket"}}, "required": []}
    with pytest.raises(dt.DynamicToolError, match="不支持"):
        _create(tool_env["registry"], input_schema=bad_type)


def test_smoke_failure_blocks_registration(tool_env):
    """smoke 测试失败 → 不注册不落盘（handoff §3.4-3 的'失败则删除并反馈'）。"""
    with pytest.raises(dt.DynamicToolError, match="smoke"):
        _create(tool_env["registry"],
                code="def handler(**kw):\n    raise ValueError('boom')\n",
                test_params={"x": 1})
    assert tool_env["registry"].get_native_tool("calc_double") is None
    assert not (tool_env["tools_dir"] / "calc_double.json").exists()


# ---------------------------------------------------------------------------
# 4. 超时守护
# ---------------------------------------------------------------------------

def test_timeout_guard(tool_env, monkeypatch):
    monkeypatch.setattr(dt, "DYNAMIC_TOOL_TIMEOUT_S", 0.2)
    _create(tool_env["registry"],
            code="import time\ndef handler(**kw):\n    time.sleep(2)\n    return 'late'\n")
    import asyncio
    result = asyncio.run(tool_env["registry"].execute("calc_double", {"x": 1}))
    assert result["success"]                    # handler 收敛为错误串而非抛异常
    assert "超时" in str(result["result"])


# ---------------------------------------------------------------------------
# 5. 重启加载（handoff DoD-2）
# ---------------------------------------------------------------------------

def test_restart_reload(tool_env):
    _create(tool_env["registry"])

    fresh = ToolRegistry()  # 模拟服务重启后的空 registry
    loaded = dt.load_dynamic_tools(fresh)
    assert loaded == ["calc_double"]
    tool = fresh.get_native_tool("calc_double")
    assert tool is not None and tool.risk_level == "high"
    import asyncio
    result = asyncio.run(fresh.execute("calc_double", {"x": 5}))
    assert result["success"] and result["result"] == "ok:5"


def test_restart_reload_skips_corrupt_tool(tool_env):
    _create(tool_env["registry"])
    (tool_env["tools_dir"] / "broken_tool.json").write_text(
        json.dumps({"name": "broken_tool", "description": "坏数据",
                    "input_schema": {"type": "object", "properties": {}}}),
        encoding="utf-8")
    # 缺 .py 文件 → 加载失败但不阻塞其余工具
    fresh = ToolRegistry()
    loaded = dt.load_dynamic_tools(fresh)
    assert loaded == ["calc_double"]
    assert fresh.get_native_tool("broken_tool") is None


# ---------------------------------------------------------------------------
# 6. 缺工具信号: 蒸馏 tool_requests → pending → create_tool 核销
# ---------------------------------------------------------------------------

def test_pending_request_saved_and_resolved(tool_env):
    saved = dt.save_pending_requests([
        {"name": "get_weather_forecast", "description": "查未来三天天气",
         "input_schema": {"type": "object",
                          "properties": {"city": {"type": "string"}}, "required": ["city"]},
         "reason": "用户连续 3 次问天气，无对应工具"},
        {"name": "Bad-Name", "description": "非法名应被跳过", "reason": "x"},
    ])
    assert saved == 1
    pendings = dt.list_pending()
    assert len(pendings) == 1 and pendings[0]["name"] == "get_weather_forecast"

    # 同名工具落地 → pending 自动核销
    _create(tool_env["registry"], name="get_weather_forecast",
            description="查未来三天天气",
            input_schema={"type": "object",
                          "properties": {"city": {"type": "string"}},
                          "required": ["city"]},
            code="def handler(**kw):\n    return 'sunny'\n")
    assert dt.list_pending() == []


def test_distill_tool_requests_flow(tool_env, monkeypatch, tmp_path):
    """蒸馏端到端: FakeModel 返回 tool_requests → pending 落盘。"""
    from app.agent.evolution import distill, experience as exp, ledger, meta

    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir()
    monkeypatch.setattr(exp, "_TRACES_DIR", traces_dir)
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    # distill_once 会写 ledger/meta_stats/seed prompt，一并隔离
    monkeypatch.setattr(meta, "_META_CONFIG", evo_dir / "meta_config.json")
    monkeypatch.setattr(meta, "_META_STATS", evo_dir / "meta_stats.json")
    monkeypatch.setattr(meta, "_PROMPTS_DIR", evo_dir / "prompts")
    monkeypatch.setattr(ledger, "_LEDGER_PATH", evo_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "_SNAPSHOTS_DIR", evo_dir / "snapshots")

    now_iso = "2026-08-31T12:00:00"
    for i in range(5):
        (traces_dir / f"trace_{i}.json").write_text(json.dumps({
            "trace_id": f"trace_{i}", "task_type": "chatbot",
            "timestamp": now_iso, "user_intent": f"查天气 {i}",
            "latency_ms": 1000, "total_tokens": 100, "success": True,
        }, ensure_ascii=False), encoding="utf-8")

    class _FakeModel:
        class _Resp:
            content = json.dumps({
                "experiences": [], "policy_suggestions": [],
                "tool_requests": [{"name": "get_weather_forecast",
                                   "description": "查天气",
                                   "input_schema": {"type": "object",
                                                    "properties": {"city": {"type": "string"}}},
                                   "reason": "repeated blocked task"}],
            }, ensure_ascii=False)

        def invoke(self, prompt):
            return self._Resp()

    monkeypatch.setattr(distill, "get_chat_model", lambda **kw: _FakeModel())
    r = distill.distill_once(force=True)
    assert r["success"] and r["tool_requests"] == 1 and r["pending_saved"] == 1
    names = [p["name"] for p in dt.list_pending()]
    assert "get_weather_forecast" in names


# ---------------------------------------------------------------------------
# 7. create_tool 原生工具: 注册 + 通过 registry.execute 全链路
# ---------------------------------------------------------------------------

def test_create_tool_native_registered(tool_env):
    from app.tool_registry.dynamic_tools import register_create_tool
    register_create_tool(tool_env["registry"])
    t = tool_env["registry"].get_native_tool("create_tool")
    assert t is not None and t.risk_level == "medium"
    assert any(tool["name"] == "create_tool"
               for tool in tool_env["registry"].list_tools_for_llm())
    # 重复注册是幂等的
    register_create_tool(tool_env["registry"])
    assert tool_env["registry"].get_native_tool("create_tool") is t


def test_create_tool_via_registry_execute(tool_env):
    """LLM 视角全链路: execute('create_tool') → 新工具下一轮可被 execute。"""
    from app.tool_registry.dynamic_tools import register_create_tool
    register_create_tool(tool_env["registry"])

    import asyncio
    r1 = asyncio.run(tool_env["registry"].execute("create_tool", {
        "name": "calc_double", "description": "测试用计算工具，翻倍输入",
        "input_schema": GOOD_SCHEMA, "code": GOOD_CODE,
    }))
    assert r1["success"] and "✅" in str(r1["result"])

    r2 = asyncio.run(tool_env["registry"].execute("calc_double", {"x": 8}))
    assert r2["success"] and r2["result"] == "ok:8"

    # 非法代码: 返回 ❌ 错误串（不抛异常，LLM 能读到原因）
    r3 = asyncio.run(tool_env["registry"].execute("create_tool", {
        "name": "evil_tool", "description": "测试非法代码",
        "input_schema": {"type": "object", "properties": {}},
        "code": "def handler(**kw):\n    import os\n    return os.getcwd()\n",
    }))
    assert r3["success"] and "❌" in str(r3["result"]) and "白名单" in str(r3["result"])
    assert tool_env["registry"].get_native_tool("evil_tool") is None


# ---------------------------------------------------------------------------
# 8. 状态汇总
# ---------------------------------------------------------------------------

def test_summary_counts(tool_env):
    s = dt.summary()
    assert s["count"] == 0 and s["pending_count"] == 0
    _create(tool_env["registry"])
    dt.save_pending_requests([{"name": "req_one", "description": "x", "reason": "y"}])
    s = dt.summary()
    assert s["count"] == 1 and s["names"] == ["calc_double"]
    assert s["pending_count"] == 1 and s["pending"][0]["name"] == "req_one"


def test_runner_status_includes_dynamic_tools(tool_env, monkeypatch, tmp_path):
    """runner.status() 聚合动态工具数量（handoff DoD-6 的数据源）。"""
    from app.agent.evolution import runner
    from app.agent.evolution import experience as exp, update

    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir()
    monkeypatch.setattr(exp, "_STATE_DIR", evo_dir)
    monkeypatch.setattr(exp, "_STATE_PATH", evo_dir / "state.json")
    monkeypatch.setattr(update, "_POLICIES_JSON", evo_dir / "policies.json")

    s = runner.status()
    assert "dynamic_tools" in s and s["dynamic_tools"]["count"] == 0
    _create(tool_env["registry"])
    s = runner.status()
    assert s["dynamic_tools"]["count"] == 1
