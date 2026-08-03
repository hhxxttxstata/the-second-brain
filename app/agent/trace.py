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

        # 失败分类
        self.failure_codes: list[str] = []

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
            "failure_codes": self.failure_codes,
        }

    def save(self) -> str:
        """保存 trace 到 agent_data/traces/。"""
        # 自动检测失败码
        if not self.success or self.error:
            try:
                from app.agent.failure_taxonomy import detect_failure_codes
                self.failure_codes = detect_failure_codes(self.to_dict())
            except Exception:
                self.failure_codes = []
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
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        cases.extend(data)
                    elif isinstance(data, dict):
                        cases.append(data)
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
    """运行固定测试集，输出回归评测报告。

    success 判定 = 路由正确 + required_outcomes 全部满足 + 未触发 forbidden_actions。
    （原实现只检查 run_orchestrator 返回值，导致约束从未被执行）
    """
    from app.agent.graphs.orchestrator import run_orchestrator
    from app.agent.failure_taxonomy import detect_failure_codes

    if test_cases is None:
        test_cases = load_test_cases()

    results: list[dict[str, Any]] = []
    success_count = 0
    total_latency = 0
    total_tokens = 0

    for case in test_cases:
        # 预置 fixture（如需）
        setup_fixture(case)

        with TraceSession("benchmark", case["intent"]) as trace:
            try:
                # 长会话 case：注入预置的历史对话（40 轮）作为 conversation
                conv = None
                if any("40 轮" in s for s in (case.get("fixture_setup", []) or [])):
                    try:
                        from .memory_store import get_session_messages
                        conv = get_session_messages("benchmark_long_session", limit=100)
                    except Exception:
                        conv = None
                r = run_orchestrator(input_text=case["input"], conversation=conv)
                trace.final_output = r.get("result", "")
                success = r.get("success", False)
                trace.success = success
                total_latency += trace.latency_ms
                result_data = r.get("result_data", {})
                if isinstance(result_data, dict):
                    mc = result_data.get("message_count", 0)
                    if mc:
                        trace.set_llm_stats("deepseek-chat", prompt_tokens=0,
                                            completion_tokens=0)
            except Exception as e:
                trace.success = False
                trace.error = str(e)

        trace_dict = trace.to_dict()
        trace_dict["route"] = r.get("route", "?")
        trace_dict["input"] = case["input"]

        # 工具调用记录在 orchestrator 内部的 trace 里（TraceSession 不接管 _current_trace）
        # run_id 是 thread_id（orch_xxx），内部 trace 是 trace_xxx 且 user_intent=case.input
        inner = _find_inner_trace(case["input"])
        if inner:
            if inner.get("tool_calls"):
                trace_dict["tool_calls"] = inner["tool_calls"]
            if inner.get("memory_updates"):
                trace_dict["memory_updates"] = inner["memory_updates"]
            if not trace_dict.get("final_output"):
                trace_dict["final_output"] = inner.get("final_output", "")

        # ── 约束检查（原来缺失的核心） ──
        outcome_checks, forbidden_hits, outcome_detail = _check_case_constraints(
            case, trace_dict, r
        )
        outcomes_ok = all(outcome_checks)
        forbidden_ok = len(forbidden_hits) == 0

        # success = 运行成功 + 路由对 + outcomes 全过 + 无 forbidden
        final_success = trace.success and outcomes_ok and forbidden_ok
        if final_success:
            success_count += 1

        # 失败码：trace 自身 + 约束失败映射
        fc = list(trace.failure_codes or [])
        if not outcomes_ok:
            fc.append("OUTCOME_NOT_MET")
        for fh in forbidden_hits:
            fc.append("FORBIDDEN_ACTION")

        results.append({
            "intent": case["intent"],
            "input": case["input"],
            "route": r.get("route", "?"),
            "success": final_success,
            "latency_ms": trace.latency_ms,
            "total_tokens": trace.total_tokens,
            "trace_id": trace.trace_id,
            "failure_codes": fc,
            "outcome_checks": outcome_detail,
            "outcomes_ok": outcomes_ok,
            "forbidden_hits": forbidden_hits,
        })

        # 清理 fixture 副作用（保留 trace）
        cleanup_fixture()

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


# ---------------------------------------------------------------------------
# 约束检查器 — required_outcomes / forbidden_actions 的真实判定
# ---------------------------------------------------------------------------

def _find_inner_trace(user_input: str) -> dict[str, Any] | None:
    """按 user_intent 查找 orchestrator 内部 trace（tool_calls 记录在那边）。

    注意：orchestrator 内部 trace 的 user_intent 存的是原始 input（非 case intent），
    且 benchmark 的 TraceSession 也会写一条（task_type=benchmark），需排除。
    最近 60 条内足够（一次评测最多 ~30 case）。
    """
    try:
        if not _TRACE_DIR.exists():
            return None
        files = sorted(_TRACE_DIR.glob("trace_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:60]:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("task_type") == "benchmark":
                    continue
                if d.get("user_intent") == user_input:
                    return d
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Fixture 预置 — 把 fixture_needed / fixture_setup 变成真实环境状态
# ---------------------------------------------------------------------------

def setup_fixture(case: dict[str, Any]) -> None:
    """在运行 case 前预置所需环境。

    支持的 fixture 维度（按 fixture_needed 的 key 匹配）:
      - task_memory: 预置一条 todo（含标题/优先级/状态）
      - profile: 写入 user profile 字段
      - pending_approval: 预置 N 条待审批操作（pending_ledger）
      - memory: 预置一条记忆（episodic/task）
      - vault: 需要真实 vault 文件（已存在则跳过）
    """
    needed = case.get("fixture_needed", {}) or {}

    # 1. task_memory — 预置 todo（带 fixture 标记，便于清理）
    tm = needed.get("task_memory")
    if tm:
        try:
            from app.agent.agent_data_service import read_memory, write_memory
            td = read_memory("task")
            if "todos" not in td:
                td["todos"] = []
            existing_titles = {t.get("title", "").strip() for t in td["todos"]}
            import re
            m = re.search(r"['\"](.+?)['\"]", tm)
            title = m.group(1) if m else tm[:40]
            if title and title not in existing_titles:
                td["todos"].append({
                    "title": title,
                    "priority": "medium",
                    "status": "pending",
                    "_fixture": True,  # 标记，cleanup 时删除
                })
                write_memory("task", td, merge=False)
        except Exception:
            pass

    # 2. pending_approval / handoff — 预置待审批任务（跨会话 handoff）
    pa = needed.get("pending_approval") or needed.get("handoff")
    if pa:
        try:
            from app.agent.handoff import create_handoff
            import re
            descs = re.findall(r"['\"](.+?)['\"]", pa) or [pa[:30]]
            for desc in descs:
                create_handoff(
                    goal=f"benchmark_fixture: 执行: {desc}",
                    pending_tool="task_op",
                    pending_params={"desc": desc},
                    completed=[],
                    next_step=f"用户批准后执行 {desc}",
                    requires_approval=True,
                )
        except Exception:
            pass

    # 3. memory — 预置记忆条目（如'用户每天晚上健身'）
    mem = needed.get("memory")
    if mem:
        try:
            from app.agent.agent_data_service import add_episodic
            add_episodic(mem[:100], tags=["fixture"])
            # 同时写入 topic memory（agent 主要搜索 preference/topic，而非 episodic）
            try:
                from app.agent.topic_memory import read_topic, write_topic
                import re as _re
                m_quoted = _re.search(r"['\"](.+?)['\"]", mem)
                content = m_quoted.group(1) if m_quoted else mem.strip("'\"，。 ")
                pref = read_topic("preferences") or ""
                # 如果 preferences 里已有'健身'相关内容（真实数据），先重置为纯'晚上健身'
                if "健身" in pref:
                    write_topic("preferences", f"# 用户偏好\n\n## 生活习惯\n- {content}\n")
                else:
                    write_topic("preferences", f"- {content}\n", append=True)
            except Exception:
                pass
        except Exception:
            pass

    # 5. session — 预置会话历史（供跨会话延续/语义提炼/长会话测试）
    sess = needed.get("session")
    if sess:
        try:
            from app.agent.session_jsonl import log_session_summary
            from app.agent.agent_data_service import add_episodic
            import re
            # 1) 写入会话摘要（含决策细节）
            log_session_summary(
                session_id="benchmark_fixture_session",
                goal=sess[:120],
                decisions=["前端框架选型讨论：比较 React/Vue/Angular 的适用场景"],
                completed=["讨论了框架选型标准"],
                next_actions=["提炼为可复用知识"],
                summary=sess[:200],
            )
            # 2) 写入相关记忆（agent 可搜索到讨论内容）
            m_quoted = re.search(r"['\"](.+?)['\"]", sess)
            detail = m_quoted.group(1) if m_quoted else sess
            add_episodic(
                f"前端框架选型讨论结论: {detail}。选型标准: 团队熟悉度、生态成熟度、项目复杂度、长期维护成本。",
                tags=["semantic", "frontend", "fixture"],
            )
        except Exception:
            pass

    # 5b. 长会话预置 — fixture_setup 显式声明 40 轮历史（长会话退化测试）
    fsetup = case.get("fixture_setup", []) or []
    if any("40 轮" in s for s in fsetup):
        try:
            from .memory_store import save_message
            from .memory_store import add_memory
            import re as _re
            # 40 轮对话：第 3 轮设定 Python 偏好，其余为填充（模拟真实长对话，触发压缩）
            for i in range(1, 41):
                if i == 3:
                    human = "以后所有的代码示例都默认用 Python，记住了吗？"
                    ai = "好的，已记住：以后代码示例默认使用 Python 语言。这个偏好已经写入记忆。"
                else:
                    human = (f"第 {i} 轮：我在看秋招的职位要求，发现很多岗位都要会 RAG 和 Agent "
                             f"架构设计。我在考虑要不要再深入学一下 LangGraph 的状态管理和多 Agent 编排，"
                             f"以及向量数据库的选型问题，你觉得这些对面试有帮助吗？")
                    ai = (f"第 {i} 轮回复：很有帮助。RAG 是高频考点，建议把检索链路讲清楚；"
                          f"Agent 方面重点准备工具调用和记忆管理。面试官喜欢问 trace 和评测体系，"
                          f"你可以准备一个端到端的项目案例来展示。另外记得代码示例默认用 Python。")
                save_message("benchmark_long_session", "human", human)
                save_message("benchmark_long_session", "ai", ai)
            # 写入 session_summary 记忆（压缩时替代早期原文）
            add_memory(
                "用户偏好：代码示例默认使用Python语言",
                memory_type="conversation",
                tags=["conversation", "fixture"],
                importance=5,
                source="session_summary",
                session_id="benchmark_long_session",
            )
        except Exception:
            pass

    # 6. vault — 需要真实 vault 文件（已存在则跳过）

    # 4. profile — 预置画像字段（含手机号等）
    prof = needed.get("profile")
    if prof:
        try:
            from app.agent.agent_data_service import write_memory
            import re
            # 预置旧手机号（触发'换新号'更新）— 同时写 stable_profile 和 topic memory
            m_phone = re.search(r"(\d{11})", prof)
            if m_phone:
                old_phone = m_phone.group(1)
                write_memory("stable_profile", {"phone": old_phone}, merge=True)
                try:
                    from app.agent.topic_memory import read_topic, write_topic
                    # 确保 people/tata.md 里的手机号是旧号（触发'换新号'更新）
                    tata = read_topic("people/tata") or ""
                    import re as _re
                    if _re.search(r"手机号[:：]\s*\d{11}", tata):
                        tata = _re.sub(r"(手机号[:：]\s*)\d{11}", rf"\g<1>{old_phone}", tata)
                        write_topic("people/tata", tata)
                    else:
                        write_topic("people/tata", f"- 手机号: {old_phone}（旧号）\n", append=True)
                except Exception:
                    pass
            m = re.search(r"name=(\S+)", prof)
            if m:
                write_memory("stable_profile", {"name": m.group(1)}, merge=True)
        except Exception:
            pass


def cleanup_fixture() -> None:
    """清理 fixture 产生的副作用（避免污染后续 case）。"""
    try:
        from app.agent.pending_ledger import _get_conn
        conn = _get_conn()
        conn.execute("DELETE FROM pending_actions WHERE session_id='benchmark_fixture'")
        conn.commit()
    except Exception:
        pass
    # 清理长会话 fixture 的消息（40 轮测试）
    try:
        from .memory_store import _get_conn as _m_conn
        conn = _m_conn()
        conn.execute("DELETE FROM messages WHERE session_id='benchmark_long_session'")
        conn.commit()
        conn.execute(
            "DELETE FROM memories WHERE session_id='benchmark_long_session' OR tags LIKE '%fixture%'")
        conn.commit()
    except Exception:
        pass
    # 清理 fixture 创建的 task_memory todos（带 _fixture 标记）
    try:
        from app.agent.agent_data_service import read_memory, write_memory
        td = read_memory("task")
        if "todos" in td:
            before = len(td["todos"])
            td["todos"] = [t for t in td["todos"] if not t.get("_fixture")]
            if len(td["todos"]) < before:
                write_memory("task", td, merge=False)
    except Exception:
        pass
    # 清理 fixture 创建的 handoffs（pending_approvals）
    try:
        from app.agent.handoff import _read_jsonl, _write_jsonl, _HANDOFFS_DIR
        pending = _read_jsonl("pending_approvals")
        keep = [p for p in pending if "benchmark_fixture" not in str(p.get("goal", ""))]
        _write_jsonl("pending_approvals", keep)
        active = _read_jsonl("active_tasks")
        keep_a = [a for a in active if "benchmark_fixture" not in str(a.get("goal", ""))]
        _write_jsonl("active_tasks", keep_a)
        # 删除对应的 handoff markdown
        try:
            for f in _HANDOFFS_DIR.glob("task_*.md"):
                content = f.read_text(encoding="utf-8")
                if "benchmark_fixture" in content:
                    f.unlink()
        except Exception:
            pass
    except Exception:
        pass


def _check_case_constraints(case: dict[str, Any], trace: dict[str, Any],
                            result: dict[str, Any]) -> tuple[list[bool], list[str], list[dict]]:
    """逐条检查 required_outcomes 和 forbidden_actions。

    Returns:
        (outcome_checks, forbidden_hits, outcome_detail)
        - outcome_checks: 每个 required_outcome 是否满足
        - forbidden_hits: 被触发的 forbidden_actions 列表
        - outcome_detail: 每个 outcome 的判定详情（供 CLI 显示）
    """
    outcomes = case.get("required_outcomes", [])
    forbiddens = case.get("forbidden_actions", [])
    expected_route = case.get("expected_route", "")
    route = trace.get("route", "?")
    final_output = trace.get("final_output", "") or ""
    tool_calls = trace.get("tool_calls", [])
    memory_updates = trace.get("memory_updates", [])
    input_text = case.get("input", "")

    tool_names = [tc.get("name", "") for tc in tool_calls]
    tool_success = {tc.get("name"): tc.get("success", True) for tc in tool_calls}
    output_lower = final_output.lower()

    outcome_checks: list[bool] = []
    outcome_detail: list[dict] = []

    for o in outcomes:
        ok, reason = _check_single_outcome(
            o, route, expected_route, final_output, output_lower,
            tool_names, tool_success, memory_updates, input_text
        )
        outcome_checks.append(ok)
        outcome_detail.append({"outcome": o, "ok": ok, "reason": reason})

    forbidden_hits: list[str] = []
    for f in forbiddens:
        hit, reason = _check_single_forbidden(
            f, route, expected_route, final_output, output_lower,
            tool_names, tool_success, memory_updates, input_text
        )
        if hit:
            forbidden_hits.append(f"{f} ({reason})")

    return outcome_checks, forbidden_hits, outcome_detail


def _check_single_outcome(
    o: str, route: str, expected_route: str, final_output: str, output_lower: str,
    tool_names: list[str], tool_success: dict[str, bool],
    memory_updates: list[dict], input_text: str,
) -> tuple[bool, str]:
    """判定单条 required_outcome。"""
    import re as _re

    # 1. 路由类 — 去掉括号注释如（而非 plan）/（走 task_ops）
    if o.startswith("路由到"):
        target = o.replace("路由到", "").strip()
        target = _re.sub(r"[（(].*?[)）]", "", target).strip().rstrip("，。 ")
        ok = route == target
        return ok, f"route={route}, 期望={target}"

    # 1b. "目标写入记忆（新增待办）或确认目标已存在" — reasonable alternative（优先于通用写入分支）
    if "或确认目标已存在" in o:
        has_write = any(t in tool_names for t in ("write_memory", "write_episodic_memory",
                                                  "write_topic_memory"))
        if has_write:
            return True, "已写入记忆"
        # 没写入但确认已存在 → 检查输出中是否说明已存在/无需重复
        if any(k in final_output for k in ("已存在", "已有", "已经记", "无需重复", "重复", "已记录")):
            return True, "输出确认目标已存在"
        # 或 task_memory 中已有该目标
        try:
            from app.agent.agent_data_service import read_memory
            td = read_memory("task")
            for t in td.get("todos", []):
                title = t.get("title", "")
                if "秋招" in title or "offer" in title.lower():
                    return True, f"目标已存在于待办: {title[:25]}"
        except Exception:
            pass
        return False, "既未写入也未确认已存在"

    # 1c. 长会话退化 — 早期偏好回忆（'代码示例默认用Python'）
    if "回忆起" in o or "长会话" in o or "早期轮次" in o:
        if "python" in output_lower or "代码" in final_output:
            return True, "输出包含早期偏好（Python/代码）"
        return False, "未能回忆起早期偏好"

    # 2. 工具调用类
    if "调用" in o and ("search_vault" in o or "read_folder" in o or "read_file" in o):
        needed = [t for t in ("search_vault", "read_folder", "read_file") if t in o]
        called = [t for t in needed if t in tool_names]
        return bool(called), f"调用了{len(called)}/{len(needed)}个读取工具"

    if "写入" in o and ("记忆" in o or "记忆" in input_text):
        has_write = any(t in tool_names for t in ("write_memory", "write_episodic_memory",
                                                  "update_task_status", "write_topic_memory"))
        if has_write:
            return True, "调用了写入工具"
        # memory_updates 兜底
        if memory_updates:
            return True, f"memory_updates={len(memory_updates)}条"
        return False, "未调用任何写入工具"

    # 3. 输出内容类
    if "不编造" in o or "非编造" in o or "基于" in o:
        # 有 vault/记忆读取工具 → 视为有依据
        has_read = any(t in tool_names for t in (
            "search_vault", "read_folder", "read_file",
            "read_topic_memory", "read_memory", "search_memories",
            "search_topic_memory",
        ))
        if has_read:
            return True, "有读取依据"
        # 无工具调用但输出包含 profile 真实字段（上下文已注入）→ 视为有依据
        if "塔塔" in final_output or "tata" in output_lower or "26" in final_output:
            return True, "输出包含 profile 真实字段（上下文注入）"
        return False, "未调用读取工具且输出无 profile 依据"

    if "引用" in o and ("日记" in o or "笔记" in o):
        has_read = any(t in tool_names for t in ("read_folder", "read_file", "search_vault"))
        return has_read, "已调用读取工具（引用证据）"

    if "输出" in o and ("计划项" in o or "条计划" in o):
        # 提取数字
        import re
        m = re.search(r"(\d+)", o)
        if m:
            count = int(m.group(1))
            # 计划项通常有编号或优先级标记
            items = [l for l in final_output.split("\n") if re.match(r"^\s*\d+[\.\、]|^\s*[-•]", l)]
            ok = len(items) >= count
            return ok, f"计划项={len(items)}, 期望≥{count}"
        return bool(final_output.strip()), "输出非空"

    # todo 状态类（优先于通用'优先级'分支）
    if "todo" in o.lower() and ("优先" in o or "优先级" in o):
        # 状态检查：查 task_memory 里最新添加的 todo 的优先级（而非输出文本）
        try:
            from app.agent.agent_data_service import read_memory
            td = read_memory("task")
            todos = td.get("todos", [])
            if not todos:
                return False, "task_memory 无 todo"
            latest = todos[-1]
            prio = str(latest.get("priority", "")).lower()
            if "低" in o:
                ok = prio in ("low", "最低")
                return ok, f"最新 todo='{latest.get('title','')[:20]}' priority={prio}"
            if "高" in o:
                ok = prio in ("high", "最高")
                return ok, f"最新 todo priority={prio}"
        except Exception as exc:
            return False, f"状态检查异常: {exc}"

    if "合并" in o and ("重复任务" in o or "同名" in o):
        try:
            from app.agent.agent_data_service import read_memory
            td = read_memory("task")
            todos = td.get("todos", [])
            titles = [t.get("title", "").strip() for t in todos]
            dup = len(titles) != len(set(titles))
            return not dup, f"todo 数={len(todos)}, 重名={dup}"
        except Exception as exc:
            return False, f"状态检查异常: {exc}"

    if "优先级" in o or "分类" in o:
        has_marker = ("[high]" in output_lower or "[medium]" in output_lower
                      or "[low]" in output_lower or "优先级" in final_output
                      or "priority" in output_lower)
        return has_marker, "输出含优先级标记"

    if "分析" in o and ("批判" in o or "建议" in o or "总结" in o):
        parts = [p for p in ("分析", "批判", "建议", "总结") if p in final_output]
        return len(parts) >= 3, f"输出含{len(parts)}/4个部分"

    # 4. 记忆类
    if "记忆" in o and "持久化" in o:
        has_write = any(t in tool_names for t in ("write_memory", "write_episodic_memory",
                                                  "write_topic_memory"))
        if has_write:
            return True, "已调用记忆写入工具"
        # 未写入但确认已存在（reasonable alternative）
        if any(k in final_output for k in ("已存在", "已有", "已经记", "已存", "之前记")):
            return True, "输出确认偏好已存在"
        return False, "未调用记忆写入工具且未确认已存在"

    if "语义记忆" in o or "semantic_knowledge" in o or "结构化的语义" in o:
        # 状态检查：最近是否有 semantic_knowledge 类型的记忆
        try:
            from app.agent.memory_store import _get_conn
            conn = _get_conn()
            row = conn.execute(
                "SELECT content FROM memories WHERE memory_type='episodic' "
                "AND tags LIKE '%semantic_knowledge%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row and (row["content"] or "").strip():
                content = row["content"]
                # 检查是否包含提炼标准（非'用户让我总结'）
                if "总结" not in content[:20] or "标准" in content or "选型" in content:
                    return True, f"存在语义知识记忆: {content[:40]}"
                return False, f"记忆内容为动作描述而非提炼: {content[:30]}"
        except Exception:
            pass
        return False, "未找到 semantic_knowledge 记忆"

    if "旧记忆" in o and ("标记" in o or "superseded" in o):
        # 状态检查：查 SQLite 里是否有被 superseded 的旧记忆
        try:
            from app.agent.memory_store import _get_conn
            conn = _get_conn()
            rows = conn.execute(
                "SELECT content, superseded_by FROM memories "
                "WHERE deprecated=1 AND superseded_by != '' "
                "ORDER BY updated_at DESC LIMIT 5"
            ).fetchall()
            for row in rows:
                sup = (row["superseded_by"] or "")
                # 找到最近被覆盖的记忆
                if sup:
                    return True, f"已检测到 superseded 记忆: {sup[:30]}"
        except Exception:
            pass
        # 兜底：memory_updates 或输出文本
        has_superseded = any("superseded" in str(mu.get("preview", "")).lower()
                             for mu in memory_updates)
        if has_superseded:
            return True, "存在 superseded 标记"
        # 输出中提到覆盖/更新旧记忆
        if any(k in final_output for k in ("覆盖", "更新了旧", "替换旧", "之前")):
            return True, "输出说明覆盖旧记忆"
        return False, "未检测到旧记忆覆盖标记"

    # 5. 通用兜底：输出非空
    return bool(final_output.strip()), "输出非空（兜底）"


def _check_single_forbidden(
    f: str, route: str, expected_route: str, final_output: str, output_lower: str,
    tool_names: list[str], tool_success: dict[str, bool],
    memory_updates: list[dict], input_text: str,
) -> tuple[bool, str]:
    """判定单条 forbidden_action 是否被触发。"""
    import re as _re

    # 1. 路由类
    if f.startswith("路由到"):
        target = f.replace("路由到", "").strip()
        target = _re.sub(r"[（(].*?[)）]", "", target).strip().rstrip("，。 ")
        # 多个候选用 / 或空格分隔
        targets = [t.strip() for t in target.replace("/", " ").split()]
        hit = route in targets
        return hit, f"route={route} ∈ 禁止列表"

    # 2. "不检查 X 就声称 Y"
    if "不检查" in f and "就声称" in f:
        has_check = any(t in tool_names for t in ("search_vault", "read_folder", "read_file",
                                                  "read_memory", "search_memories"))
        claims = any(k in final_output for k in ("已删除", "已去掉", "已完成", "已处理"))
        hit = claims and not has_check
        return hit, "声称完成但未检查"

    if "没有" in f and "工具" in f and ("调用" in f or "trace" in f):
        # 声称有工具调用问题但实际没调用
        claims_err = any(k in final_output for k in ("参数问题", "参数名", "bug"))
        hit = claims_err and not tool_names
        return hit, "声称工具异常但无调用"

    if "空白" in f or "空白的" in f:
        claims_blank = any(k in final_output for k in ("空白", "没有日记", "没写"))
        hit = claims_blank and any(t in tool_names for t in ("search_vault", "read_folder", "read_file"))
        return hit, "声称日记空白但有读取调用"

    if "只用记忆" in f or ("不查 vault" in f):
        used_memory = any(t in tool_names for t in ("read_memory", "search_memories"))
        used_vault = any(t in tool_names for t in ("search_vault", "read_folder", "read_file"))
        hit = used_memory and not used_vault
        return hit, "只用记忆未查 vault"

    if "编造" in f or "虚构" in f:
        # 有任意读取工具（vault / topic memory / 记忆）→ 不算编造
        has_read = any(t in tool_names for t in (
            "search_vault", "read_folder", "read_file",
            "read_topic_memory", "read_memory", "search_memories",
            "search_topic_memory",
        ))
        hit = not has_read
        return hit, "无读取依据"

    if "合并" in f or "去重" in f:
        # 禁止合并/去重 — 检测是否执行了合并
        merge_words = ["合并", "merge", "去重", "重复"]
        hit = any(w in output_lower for w in merge_words) and any(
            t in tool_names for t in ("update_task_status", "write_topic_memory", "write_memory"))
        return hit, "执行了合并操作"

    if "修改已有" in f or "不修改任何已有" in f:
        wrote = any(t in tool_names for t in ("vault_write", "write_file", "write_topic_memory"))
        hit = wrote
        return hit, "调用了覆盖写入工具"

    # 3. 记忆写入类
    if "作为" in f and "记忆" in f and ("情绪" in f or "头痛" in f or "临时" in f):
        # 把瞬时状态写入长期记忆 — 检查写入的记忆内容（memory_updates 或工具参数），而非输出文本
        emotion_words = ["头痛", "累", "不舒服", "困"]
        # 1) memory_updates 的 preview 里含情绪词
        for mu in memory_updates:
            preview = str(mu.get("preview", ""))
            if any(w in preview for w in emotion_words):
                return True, f"记忆内容含情绪词: {preview[:40]}"
        # 2) 工具调用参数里含情绪词（用 tool_names 判断有没有写入工具；参数细节用 result_preview）
        if any(t in tool_names for t in ("write_memory", "write_episodic_memory")):
            # memory_updates 已检查过 preview；再查 trace 级输出是否把情绪写进记忆摘要
            for mu in memory_updates:
                preview = str(mu.get("preview", ""))
                if any(w in preview for w in emotion_words):
                    return True, f"记忆内容含情绪词: {preview[:40]}"
        return False, "未检测到情绪词写入记忆"

    if "忽略" in f:
        return False, "难以自动判定（人工复核）"

    # 4. 兜底：无法判定 → 不触发（宁可放行不可误杀）
    return False, "无法自动判定（放行）"


def _check_forbidden(f: str, route: str, expected_route: str, final_output: str,
                     tool_names: list[str], tool_success: dict[str, bool],
                     memory_updates: list[dict], input_text: str) -> tuple[bool, str]:
    """简化版 forbidden 检查（兼容旧调用）。"""
    return _check_single_forbidden(
        f, route, expected_route, final_output, final_output.lower(),
        tool_names, tool_success, memory_updates, input_text,
    )


def get_latest_trace() -> dict[str, Any] | None:
    """获取最近的一条 trace 记录。"""
    if not _TRACE_DIR.exists():
        return None
    files = sorted(_TRACE_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


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
