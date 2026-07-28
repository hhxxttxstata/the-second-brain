"""Agent Trace — 完整的运行轨迹记录系统。

每次 Agent 运行记录：
- 用户意图（意图分类）
- 读取了哪些上下文（来源层 + 类型）
- 调用了哪些工具（名称、参数、耗时、是否成功）
- 是否产生了记忆更新
- token / latency / 是否完成任务
- 最终输出摘要
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.core.config import settings

_TRACE_DIR = settings.agent_data_dir / "traces"
_BENCHMARK_DIR = settings.agent_data_dir / "benchmark"


def _ensure_dir() -> Path:
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    return _TRACE_DIR


# ---------------------------------------------------------------------------
# TraceRecord — 单次 Agent 运行的完整记录
# ---------------------------------------------------------------------------

# 全局当前 trace（用于自动记录工具调用和记忆写入，避免在子 agent 内部传参）
_current_trace: "TraceRecord | None" = None


def set_current_trace(trace: "TraceRecord | None") -> None:
    """设置/清除当前 agent 运行的 trace 引用。"""
    global _current_trace
    _current_trace = trace


def get_current_trace() -> "TraceRecord | None":
    return _current_trace


class TraceRecord:
    """Agent 运行的完整轨迹。"""

    def __init__(self, task_type: str, user_intent: str = "") -> None:
        self.trace_id = f"trace_{uuid.uuid4().hex[:10]}"
        self.task_type = task_type
        self.user_intent = user_intent
        self.timestamp = datetime.now().isoformat()

        # 上下文
        self.context_sources: list[dict[str, Any]] = []    # {layer, type, chars, content_preview}
        self.referenced_notes: list[str] = []              # vault 中引用的笔记路径

        # 工具调用
        self.tool_calls: list[dict[str, Any]] = []

        # LLM 信息
        self.llm_model: str = ""
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0

        # 结果
        self.final_output: str = ""
        self.success: bool = True
        self.error: str | None = None
        self.latency_ms: int = 0

        # 记忆更新
        self.memory_updates: list[dict[str, Any]] = []

        # 人工确认
        self.required_confirmation: bool = False
        self.confirmed: bool | None = None

        # 耗时
        self._start_time: float | None = None
        self._end_time: float | None = None

    def start(self) -> None:
        self._start_time = time.monotonic()

    def stop(self) -> int:
        self._end_time = time.monotonic()
        self.latency_ms = int((self._end_time - self._start_time) * 1000) if self._start_time else 0
        return self.latency_ms

    def add_context_source(self, layer: str, source_type: str, chars: int,
                           content_preview: str = "") -> None:
        self.context_sources.append({
            "layer": layer,
            "type": source_type,
            "chars": chars,
            "preview": content_preview[:100],
        })

    def add_referenced_note(self, path: str) -> None:
        if path not in self.referenced_notes:
            self.referenced_notes.append(path)

    def add_tool_call(self, name: str, params: dict[str, Any],
                      result_summary: str, latency_ms: int,
                      success: bool, error: str | None = None) -> None:
        self.tool_calls.append({
            "name": name,
            "params": {k: str(v)[:100] for k, v in params.items()},
            "result_preview": str(result_summary)[:200],
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
        })

    def add_memory_update(self, memory_type: str, content_preview: str) -> None:
        self.memory_updates.append({
            "type": memory_type,
            "preview": content_preview[:100],
            "timestamp": datetime.now().isoformat(),
        })

    def set_llm_stats(self, model: str, prompt_tokens: int = 0,
                      completion_tokens: int = 0) -> None:
        self.llm_model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def set_required_confirmation(self, required: bool = True) -> None:
        self.required_confirmation = required

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "task_type": self.task_type,
            "user_intent": self.user_intent,
            "timestamp": self.timestamp,
            "context_sources": self.context_sources,
            "referenced_notes": self.referenced_notes,
            "tool_calls": self.tool_calls,
            "llm_model": self.llm_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "final_output": self.final_output[:500],
            "success": self.success,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "memory_updates": self.memory_updates,
            "required_confirmation": self.required_confirmation,
            "confirmed": self.confirmed,
        }

    def save(self) -> str:
        """保存 trace 到 agent_data/traces/。"""
        path = _ensure_dir() / f"{self.trace_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.trace_id


# ---------------------------------------------------------------------------
# TraceSession — 用上下文管理器包裹整个 Agent 调用
# ---------------------------------------------------------------------------

class TraceSession:
    """包裹一次 Agent 运行的 trace 上下文。

    用法:
        with TraceSession("plan") as trace:
            trace.add_context_source("agent_data", "profile", 500)
            # ... 执行逻辑 ...
            trace.add_tool_call("search_vault", {"keyword": "秋招"}, "...", 200, True)
    """

    def __init__(self, task_type: str, user_intent: str = "") -> None:
        self.record = TraceRecord(task_type, user_intent)

    def __enter__(self) -> TraceRecord:
        self.record.start()
        return self.record

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.record.stop()
        if exc_type is not None:
            self.record.success = False
            self.record.error = str(exc_val)
        self.record.save()


# ---------------------------------------------------------------------------
# Benchmark / 回归测试 — 对固定测试集跑分
# ---------------------------------------------------------------------------

def load_test_cases(tier: str | None = None,
                     path: Path | str | None = None) -> list[dict[str, Any]]:
    """从评测集读取测试用例。

    Args:
        tier: "regression" | "golden" | "challenge" | "exploratory" | "candidate" | "all"
              默认 None = "regression"
        path: 直接指定文件路径，覆盖 tier
    """
    eval_dir = _BENCHMARK_DIR.parent / "eval"

    # 直接指定文件
    if path is not None:
        p = Path(path)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    # 按 tier 搜索
    if tier is None:
        tier = "regression"

    def _load_json_files(*globs: str) -> list[dict]:
        cases = []
        for g in globs:
            for f in sorted(eval_dir.glob(g)):
                try:
                    cases.extend(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
        return cases

    TIER_MAP: dict[str, list[str]] = {
        "regression": ["golden/regression.json"],
        "golden": ["golden/*.json"],
        "challenge": ["challenge/*.json"],
        "exploratory": ["exploratory/*.json"],
        "candidate": ["candidate/*.json"],
        "heldout": ["heldout/*.json"],
        "all": ["golden/*.json", "challenge/*.json",
                 "exploratory/*.json", "candidate/*.json",
                 "heldout/*.json"],
    }

    globs = TIER_MAP.get(tier)
    if globs is None:
        tier = "regression"
        globs = TIER_MAP["regression"]

    return _load_json_files(*globs)


def run_benchmark_suite(test_cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """运行固定测试集，输出回归评测报告。"""
    from app.agent.graphs.orchestrator import run_orchestrator

    if test_cases is None:
        test_cases = load_test_cases()

    results: list[dict[str, Any]] = []
    success_count = 0
    total_latency = 0
    total_tokens = 0

    for case in test_cases:
        with TraceSession("benchmark", case["intent"]) as trace:
            try:
                r = run_orchestrator(input_text=case["input"])
                trace.final_output = r.get("result", "")
                success = r.get("success", False)
                trace.success = success
                if success:
                    success_count += 1
                total_latency += trace.latency_ms
                # 从 context sources 记录（简化版）
                result_data = r.get("result_data", {})
                if isinstance(result_data, dict):
                    mc = result_data.get("message_count", 0)
                    if mc:
                        trace.set_llm_stats("deepseek-chat", prompt_tokens=0,
                                            completion_tokens=0)
            except Exception as e:
                trace.success = False
                trace.error = str(e)

        results.append({
            "intent": case["intent"],
            "input": case["input"],
            "route": r.get("route", "?"),
            "success": trace.success,
            "latency_ms": trace.latency_ms,
            "total_tokens": trace.total_tokens,
            "trace_id": trace.trace_id,
        })

    total = len(test_cases)
    _BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_cases": total,
        "pass_rate": round(success_count / total * 100, 1) if total else 0,
        "avg_latency_ms": round(total_latency / total) if total else 0,
        "avg_tokens": round(total_tokens / total) if total else 0,
        "results": results,
    }

    # 存到 benchmark 目录对比历史
    import re
    safe_date = re.sub(r"[^\w]", "_", datetime.now().isoformat()[:10])
    ( _BENCHMARK_DIR / f"benchmark_{safe_date}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report


def load_all_traces(limit: int = 100) -> list[dict[str, Any]]:
    """加载所有 trace 记录。"""
    if not _TRACE_DIR.exists():
        return []
    files = sorted(_TRACE_DIR.glob("trace_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    traces = []
    for f in files[:limit]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            traces.append(data)
        except Exception:
            pass
    return traces


def get_trace_stats(traces: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """从 traces 汇总统计指标。"""
    if traces is None:
        traces = load_all_traces()

    if not traces:
        return {"total": 0, "msg": "暂无 trace 数据"}

    success_count = sum(1 for t in traces if t.get("success"))
    total_latency = sum(t.get("latency_ms", 0) for t in traces)
    total_tokens = sum(t.get("total_tokens", 0) for t in traces)
    tool_call_count = sum(len(t.get("tool_calls", [])) for t in traces)
    memory_updates = sum(len(t.get("memory_updates", [])) for t in traces)

    # 按 task_type 分组
    by_type: dict[str, int] = {}
    for t in traces:
        tt = t.get("task_type", "unknown")
        by_type[tt] = by_type.get(tt, 0) + 1

    # 按工具分
    by_tool: dict[str, int] = {}
    for t in traces:
        for tc in t.get("tool_calls", []):
            name = tc.get("name", "?")
            by_tool[name] = by_tool.get(name, 0) + 1

    # 延迟分布
    latencies = [t.get("latency_ms", 0) for t in traces if t.get("latency_ms")]

    return {
        "total": len(traces),
        "success_count": success_count,
        "success_rate": round(success_count / len(traces) * 100, 1),
        "avg_latency_ms": round(total_latency / len(traces)),
        "max_latency_ms": max(latencies) if latencies else 0,
        "total_tokens": total_tokens,
        "avg_tokens": round(total_tokens / len(traces)) if traces else 0,
        "total_tool_calls": tool_call_count,
        "total_memory_updates": memory_updates,
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])[:10]),
    }
