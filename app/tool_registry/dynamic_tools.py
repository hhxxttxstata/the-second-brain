"""Dynamic tools — L3 自进化：Agent 发现工具不足时自己写工具并立即生效。

闭环（handoff.md §3.1/§3.4）:
  缺工具信号（蒸馏 tool_requests → pending/ 待审批）
    → create_tool（LLM 写 Python → compile/AST 校验 → 受限命名空间 exec）
    → 持久化 agent_data/tools/<name>.py + <name>.json
    → register_native + reset_agent_tools_cache（下一次请求图重建后 LLM 立即可见）
    → 服务重启后由 load_dynamic_tools 自动加载

安全边界（沿用现有体系，handoff §3.4-4）:
  - 代码校验: compile 语法检查 + AST 静态审查（import 白名单 / 禁用危险内建 / 禁 dunder 访问）
  - 执行隔离: 受限 __builtins__ 子集 + 自定义 __import__（仅白名单模块）+ 守护线程超时
  - 风险等级: 动态工具默认 high（高风险调用写审计文件），只读纯函数可显式 low
  - create_tool 本身 medium: LLM 写代码属敏感操作，系统提示要求先与用户确认
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.core.logging import logger
from app.tool_registry.registry import RegisteredTool, ToolRegistry

# ── 路径（monkeypatch 点：测试必须全量替换，见 tests/test_tool_evolution.py）──

_BASE_DIR = Path(__file__).resolve().parent.parent.parent / "agent_data" / "tools"
_TOOLS_DIR = _BASE_DIR
_PENDING_DIR = _BASE_DIR / "pending"

# ── 阈值常量 ──

MAX_SCHEMA_PARAMS = 5          # 单个动态工具最多 5 个参数（handoff §3.4-3）
DYNAMIC_TOOL_TIMEOUT_S = 10    # 动态工具执行超时（守护线程 join 上限）
CREATE_TOOL_NAME = "create_tool"

# 工具名: 小写蛇形 3-40 字符，防止奇怪标识符进文件名与 exec 命名空间
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_PARAM_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
_ALLOWED_PARAM_TYPES = {"string", "integer", "number", "boolean", "array", "object"}

# import 白名单（handoff §3.4-4）: 标准库 + 项目内模块在命名空间单独注入
_ALLOWED_IMPORT_ROOTS = {"json", "math", "re", "datetime", "time"}

# 禁止调用的内建名（AST 层拒绝；print 特殊提示）
_FORBIDDEN_BUILTINS = {
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "delattr", "setattr", "getattr", "print",
}

# 受限 __builtins__ 子集：只保留数据处理所需的只读内建 + 异常类
import builtins as _builtins_mod

_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(_builtins_mod, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "format", "frozenset", "int", "isinstance", "issubclass",
        "len", "list", "map", "max", "min", "pow", "range", "repr", "round",
        "set", "sorted", "str", "sum", "tuple", "zip",
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "RuntimeError", "StopIteration", "ArithmeticError", "AttributeError",
    )
}


class DynamicToolError(Exception):
    """动态工具创建/加载失败（错误信息直接反馈给 LLM）。"""


# ============================================================================
# 代码校验与受限执行
# ============================================================================

def _validate_name(name: str, registry: ToolRegistry) -> None:
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise DynamicToolError(
            f"工具名 '{name}' 不合法：需小写蛇形 3-40 字符（如 get_weather_forecast）")
    if name == CREATE_TOOL_NAME:
        raise DynamicToolError("不能覆盖内置工具 create_tool")
    if registry.get_native_tool(name) is not None:
        raise DynamicToolError(f"工具名 '{name}' 已存在（与现有工具冲突），请换名或先删除")


def _validate_schema(input_schema: Any) -> dict[str, Any]:
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise DynamicToolError("input_schema 必须是 {\"type\": \"object\", ...} 的 JSON Schema")
    props = input_schema.get("properties")
    if not isinstance(props, dict):
        raise DynamicToolError("input_schema.properties 缺失（需声明参数）")
    if len(props) > MAX_SCHEMA_PARAMS:
        raise DynamicToolError(f"参数过多（{len(props)} > {MAX_SCHEMA_PARAMS}），动态工具应保持单一职责")
    for pname, pdef in props.items():
        if not isinstance(pname, str) or not _PARAM_PATTERN.match(pname):
            raise DynamicToolError(f"参数名 '{pname}' 不合法：需小写蛇形标识符")
        ptype = (pdef or {}).get("type", "string")
        if ptype not in _ALLOWED_PARAM_TYPES:
            raise DynamicToolError(f"参数 '{pname}' 的类型 '{ptype}' 不支持"
                                   f"（可选: {sorted(_ALLOWED_PARAM_TYPES)}）")
    required = input_schema.get("required", [])
    if not isinstance(required, list) or any(r not in props for r in required):
        raise DynamicToolError("input_schema.required 必须是 properties 键的子集")
    return {"type": "object", "properties": props, "required": list(required)}


def _validate_code_ast(tree: ast.AST) -> None:
    """AST 静态审查：import 白名单 / 危险内建 / dunder 访问。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_IMPORT_ROOTS:
                    raise DynamicToolError(
                        f"import '{alias.name}' 不在白名单 {sorted(_ALLOWED_IMPORT_ROOTS)}"
                        "（vault/ads 已在命名空间中，无需 import）")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                raise DynamicToolError("不允许相对导入")
            root = (node.module or "").split(".")[0]
            if root not in _ALLOWED_IMPORT_ROOTS:
                raise DynamicToolError(
                    f"from '{node.module}' import 不在白名单 {sorted(_ALLOWED_IMPORT_ROOTS)}"
                    "（vault/ads 已在命名空间中，无需 import）")
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_BUILTINS:
                hint = "，请用 return 返回结果" if node.id == "print" else ""
                raise DynamicToolError(f"代码使用了禁止的内建 '{node.id}'{hint}")
            if node.id.startswith("__"):
                raise DynamicToolError(f"禁止访问双下划线名称 '{node.id}'")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise DynamicToolError(f"禁止访问双下划线属性 '{node.attr}'（防沙箱逃逸）")


def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
    """受限 __import__：运行时兜底（创建时 AST 已拦截一次）。"""
    import importlib
    root = name.split(".")[0]
    if root not in _ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"import '{name}' 不在白名单 {sorted(_ALLOWED_IMPORT_ROOTS)}")
    return importlib.import_module(name)


def _build_namespace() -> dict[str, Any]:
    """受限 exec 命名空间：白名单内建 + 预注入模块（handoff §3.4-3/4）。"""
    ns: dict[str, Any] = {
        "__builtins__": {**_SAFE_BUILTINS, "__import__": _safe_import},
        "__name__": "dynamic_tool",
        "json": json, "math": __import__("math"), "re": re,
        "datetime": __import__("datetime"), "time": __import__("time"),
    }
    # 项目内模块延迟注入：缺失不阻塞（如 vault 目录未配置时仍可注册纯计算工具）
    try:
        from app.obsidian import vault
        ns["vault"] = vault
    except Exception:
        pass
    try:
        from app.agent import agent_data_service as ads
        ns["ads"] = ads
    except Exception:
        pass
    try:
        import requests
        ns["requests"] = requests  # 可选：外呼能力，失败降级为不可用
    except Exception:
        pass
    return ns


def _normalize_source(name: str, code: str) -> str:
    """兼容三种写法：完整函数定义（可带 import）、仅函数体（自动包裹进 handler 模板）。

    判定依据 AST 而非"是否以 def 开头"——LLM 最常输出 "import + def" 组合，
    按前缀判断会把整个函数当成函数体嵌套吞掉。规则:
      - 源码可解析且含顶层函数定义 → 原样使用（必须有 handler）
      - 其余（纯语句/含 return 的片段）→ 包裹为 handler 函数体
    """
    stripped = code.strip() if isinstance(code, str) else ""
    if not stripped:
        raise DynamicToolError("code 为空：请提供 handler 函数（Python 源码）")
    has_func = False
    try:
        tree = ast.parse(stripped)
        has_func = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       for n in tree.body)
    except SyntaxError:
        pass
    if has_func:
        return stripped
    body = textwrap.indent(stripped, "    ")
    wrapped = f"def handler(**kwargs) -> str:\n{body}\n"
    try:
        ast.parse(wrapped)
    except SyntaxError as exc:
        raise DynamicToolError(
            f"代码语法错误（第 {exc.lineno} 行）: {exc.msg}") from exc
    return wrapped


def _materialize_handler(name: str, code: str) -> tuple[Callable[..., str], Callable[..., str], str]:
    """校验 + exec：返回 (带超时守护的 handler, 原始 handler, 规范化源码)。

    命名空间被 handler 闭包持有，模块级预注入与函数内 import 均可用。
    原始 handler 供 smoke 测试使用（异常穿透才能发现代码缺陷）；
    注册进 registry 的是守护版（异常/超时收敛为错误串）。
    """
    src = _normalize_source(name, code)
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise DynamicToolError(
            f"代码语法错误（第 {exc.lineno} 行）: {exc.msg}") from exc
    _validate_code_ast(tree)

    ns = _build_namespace()
    try:
        exec(compile(src, f"<dynamic_tool:{name}>", "exec"), ns)  # noqa: S102 —— 命名空间受限
    except DynamicToolError:
        raise
    except Exception as exc:
        raise DynamicToolError(f"代码加载失败: {type(exc).__name__}: {exc}") from exc

    raw = ns.get("handler")
    if not callable(raw):
        raise DynamicToolError("代码必须定义 handler(**kwargs) 函数")
    if inspect.iscoroutinefunction(raw):
        raise DynamicToolError("handler 不能是 async 函数（registry 同步调用）")
    try:
        params = inspect.signature(raw).parameters.values()
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            raise DynamicToolError("handler 签名必须接受 **kwargs（工具调用按名传参）")
    except (TypeError, ValueError) as exc:
        raise DynamicToolError(f"无法解析 handler 签名: {exc}") from exc

    return _guard_timeout(name, raw), raw, src


def _guard_timeout(name: str, handler: Callable[..., str]) -> Callable[..., str]:
    """守护线程超时包装：超时返回错误串，不阻塞 registry 主流程。

    说明: Python 无法强杀运行中的线程，超时后工作线程成为孤儿 daemon，
    解释器退出时会被回收；结果以超时错误为准（与 registry 的 success 语义一致）。
    """
    def wrapped(**kwargs: Any) -> str:
        box: dict[str, str] = {}

        def _target() -> None:
            try:
                box["result"] = str(handler(**kwargs))
            except BaseException as exc:  # 用户代码任意异常都收敛为错误串
                box["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=_target, daemon=True, name=f"dyn_{name}")
        worker.start()
        worker.join(timeout=DYNAMIC_TOOL_TIMEOUT_S)
        if worker.is_alive():
            logger.error("dynamic_tool.timeout", tool=name, timeout_s=DYNAMIC_TOOL_TIMEOUT_S)
            return f"❌ 工具 {name} 执行超时（>{DYNAMIC_TOOL_TIMEOUT_S}s），已中断"
        if "error" in box:
            return f"❌ {box['error']}"
        return box.get("result", "")

    wrapped.__name__ = name
    return wrapped


# ============================================================================
# 持久化: agent_data/tools/<name>.py + <name>.json
# ============================================================================

def _tool_json_path(name: str) -> Path:
    return _TOOLS_DIR / f"{name}.json"


def _tool_py_path(name: str) -> Path:
    return _TOOLS_DIR / f"{name}.py"


def _persist_tool(name: str, meta: dict[str, Any], src: str) -> None:
    _TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    _tool_py_path(name).write_text(src, encoding="utf-8")
    _tool_json_path(name).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_tool_files(name: str) -> None:
    for p in (_tool_py_path(name), _tool_json_path(name)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def load_dynamic_tools(registry: ToolRegistry) -> list[str]:
    """启动加载：扫描 agent_data/tools/*.json 并注册（服务重启后自动生效）。

    单个工具损坏不阻塞启动：记录错误并跳过（继续加载其余工具）。
    """
    loaded: list[str] = []
    if not _TOOLS_DIR.is_dir():
        return loaded
    for meta_path in sorted(_TOOLS_DIR.glob("*.json")):
        name = meta_path.stem
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            src = _tool_py_path(name).read_text(encoding="utf-8")
            handler, _, _ = _materialize_handler(name, src)
            registry.register_native(RegisteredTool(
                name=name,
                description=str(meta.get("description", "")),
                schema_=_validate_schema(meta.get("input_schema")),
                source="dynamic", server_name=None,
                risk_level=str(meta.get("risk_level", "high")),
                side_effects=list(meta.get("side_effects") or []),
                handler=handler,
            ))
            loaded.append(name)
        except Exception as exc:
            logger.error("dynamic_tool.load_failed", tool=name, error=str(exc)[:200])
    if loaded:
        logger.info("dynamic_tools.loaded", count=len(loaded), names=loaded)
    return loaded


def count_dynamic_tools() -> int:
    """磁盘上的动态工具数（不触发注册，供状态展示）。"""
    return len(list(_TOOLS_DIR.glob("*.json"))) if _TOOLS_DIR.is_dir() else 0


# ============================================================================
# 核心入口: create_dynamic_tool（create_tool 原生工具的落点）
# ============================================================================

def create_dynamic_tool(
    registry: ToolRegistry,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    code: str,
    risk_level: str = "high",
    side_effects: list[str] | None = None,
    test_params: dict[str, Any] | None = None,
    source: str = "chatbot",
) -> dict[str, Any]:
    """创建并注册一个动态工具（全部校验通过才落盘，失败零副作用）。

    流程: 校验（名/schema/代码）→ smoke 测试（可选）→ 持久化 → 注册
          → reset_agent_tools_cache（下一次请求图重建后 LLM 立即可见）
          → 若蒸馏 pending 中有同名请求则标记已落地
    """
    if not isinstance(description, str) or len(description.strip()) < 5:
        raise DynamicToolError("description 过短（≥5 字符），LLM 需靠它判断何时调用该工具")
    _validate_name(name, registry)
    schema = _validate_schema(input_schema)
    risk_level = risk_level if risk_level in ("low", "high") else "high"
    handler, raw_handler, src = _materialize_handler(name, code)

    # smoke 测试在持久化/注册之前，直接调原始 handler（异常穿透才能暴露缺陷）：
    # 失败即整体拒绝，零副作用无需回滚
    smoke_tested = False
    if test_params:
        if not isinstance(test_params, dict):
            raise DynamicToolError("test_params 必须是对象（参数名 → 示例值）")
        try:
            raw_handler(**test_params)
            smoke_tested = True
        except Exception as exc:
            raise DynamicToolError(
                f"smoke 测试失败（工具未注册，代码未落盘）: {type(exc).__name__}: {exc}") from exc

    now = datetime.now().isoformat()
    meta = {
        "name": name,
        "description": description.strip(),
        "input_schema": schema,
        "risk_level": risk_level,
        "side_effects": side_effects or ([] if risk_level == "low" else ["Agent 生成的外部代码"]),
        "created_at": now, "updated_at": now,
        "source": source, "smoke_tested": smoke_tested,
    }

    _persist_tool(name, meta, src)
    registry.register_native(RegisteredTool(
        name=name, description=meta["description"], schema_=schema,
        source="dynamic", server_name=None,
        risk_level=risk_level, side_effects=meta["side_effects"],
        handler=handler,
    ))
    _reset_agent_tools_cache()
    _resolve_pending(name)

    logger.info("dynamic_tool.created", tool=name, risk=risk_level,
                smoke=smoke_tested, source=source)
    return {"name": name, "risk_level": risk_level,
            "smoke_tested": smoke_tested, "persisted": True}


def _reset_agent_tools_cache() -> None:
    """清 LLM 工具列表缓存（handoff §3.3 即时生效链路的第一环）。

    延迟 import 避免循环依赖: graphs.tools → native_tools → 本模块。
    """
    try:
        from app.agent.graphs.tools import reset_agent_tools_cache
        reset_agent_tools_cache()
    except Exception as exc:
        logger.error("dynamic_tool.cache_reset_failed", error=str(exc)[:200])


# ============================================================================
# 缺工具信号: 蒸馏 tool_requests → pending/ 待审批（handoff §3.4-5 方案 A）
# ============================================================================

def save_pending_requests(requests: list[dict[str, Any]]) -> int:
    """蒸馏产出的 tool_requests 落成待审批预填（create_tool 时自动核销）。"""
    if not _PENDING_DIR.exists():
        _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for req in requests[:3]:  # 每轮蒸馏至多落 3 条，防 LLM 失控刷请求
        name = str(req.get("name", "")).strip()
        if not _NAME_PATTERN.match(name):
            logger.error("dynamic_tool.pending_invalid_name", name=name[:40])
            continue
        payload = {
            "name": name,
            "description": str(req.get("description", ""))[:200],
            "input_schema": req.get("input_schema") or {"type": "object", "properties": {}, "required": []},
            "reason": str(req.get("reason", ""))[:300],
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        (_PENDING_DIR / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        saved += 1
    if saved:
        logger.info("dynamic_tool.pending_saved", count=saved)
    return saved


def list_pending() -> list[dict[str, Any]]:
    if not _PENDING_DIR.is_dir():
        return []
    out = []
    for p in sorted(_PENDING_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _resolve_pending(name: str) -> bool:
    """create_tool 落地后核销同名 pending 请求（视为审批通过）。"""
    p = _PENDING_DIR / f"{name}.json"
    if not p.exists():
        return False
    try:
        p.unlink()
        logger.info("dynamic_tool.pending_resolved", tool=name)
        return True
    except OSError:
        return False


# ============================================================================
# create_tool 原生工具（暴露给 LLM 自己调用）
# ============================================================================

_CREATE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string",
                 "description": "工具名，小写蛇形 3-40 字符，如 get_weather_forecast"},
        "description": {"type": "string",
                        "description": "一句话说明工具做什么、什么时候用（LLM 靠它决定是否调用）"},
        "input_schema": {"type": "object",
                         "description": "JSON Schema：{\"type\":\"object\",\"properties\":{...},\"required\":[...]}，≤5 个参数"},
        "code": {"type": "string",
                 "description": "Python 源码：完整 def handler(**kwargs) -> str 定义，或仅函数体（自动包裹）。可用命名空间: json/math/re/datetime/vault/ads/requests；禁止 open/import os/print"},
        "risk_level": {"type": "string", "enum": ["low", "high"],
                       "description": "默认 high（走审计）；只读纯函数可显式 low"},
        "test_params": {"type": "object",
                        "description": "可选：注册后自测的示例参数，失败则不注册"},
    },
    "required": ["name", "description", "input_schema", "code"],
}


def _create_tool_handler(**kw: Any) -> str:
    """create_tool 的 handler：把 registry 上下文延迟解析进来。"""
    try:
        result = create_dynamic_tool(
            registry=_resolve_registry(),
            name=str(kw.get("name", "")),
            description=str(kw.get("description", "")),
            input_schema=kw.get("input_schema") or {},
            code=str(kw.get("code", "")),
            risk_level=str(kw.get("risk_level") or "high"),
            test_params=kw.get("test_params"),
            source="chatbot",
        )
        return (f"✅ 动态工具已创建并生效: {result['name']}"
                f"（risk={result['risk_level']}, smoke_tested={result['smoke_tested']}）。"
                "下一轮对话即可直接调用。")
    except DynamicToolError as exc:
        return f"❌ 创建失败: {exc}"
    except Exception as exc:
        return f"❌ 创建失败: {type(exc).__name__}: {exc}"


def _resolve_registry() -> ToolRegistry:
    """运行时解析全局 registry（native_tools 无反向依赖 graphs.tools）。"""
    from app.agent.graphs.tools import get_registry
    return get_registry()


def register_create_tool(registry: ToolRegistry) -> None:
    if registry.get_native_tool(CREATE_TOOL_NAME) is not None:
        return
    registry.register_native(RegisteredTool(
        name=CREATE_TOOL_NAME,
        description=("创建新工具并立即生效——当现有工具无法完成用户任务时使用。"
                     "调用前必须先用 ask_clarification 与用户确认工具名与行为。"
                     "创建成功后下一轮对话即可调用新工具"),
        schema_=_CREATE_TOOL_SCHEMA,
        source="native", server_name=None,
        risk_level="medium",
        side_effects=["写入 agent_data/tools/ 并注册新工具（LLM 生成代码，需人工知悉）"],
        handler=_create_tool_handler,
    ))


# ============================================================================
# 状态汇总（runner.status / CLI evolve status）
# ============================================================================

def summary() -> dict[str, Any]:
    pending = list_pending()
    names = sorted(p.stem for p in _TOOLS_DIR.glob("*.json")) if _TOOLS_DIR.is_dir() else []
    return {
        "count": len(names),
        "names": names,
        "pending_count": len(pending),
        "pending": [{"name": p.get("name"), "reason": p.get("reason", "")[:60]}
                    for p in pending],
    }
