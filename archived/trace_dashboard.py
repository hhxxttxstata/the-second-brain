"""
Agent Trace Dashboard — Streamlit 面板

启动:
    streamlit run app/trace_dashboard.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import streamlit as st
from streamlit import columns, divider, metric, tabs

from app.agent.trace import get_trace_stats, load_all_traces, run_benchmark_suite
from app.agent.self_eval import run_self_eval
from app.core.config import settings

st.set_page_config(
    page_title="Agent Trace & Eval",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.markdown("# 🔍 Agent 可观测性")
st.sidebar.markdown("Obsidian-native Agent · DeepSeek · LangGraph")
st.sidebar.divider()

page = st.sidebar.radio(
    "导航",
    ["📊 概览", "📋 Trace 列表", "📈 回归评测", "🛠️ 自评测", "⚙️ 设置"],
)

st.sidebar.divider()
st.sidebar.markdown(f"**Vault**: {settings.obsidian_vault}")
st.sidebar.markdown(f"**Model**: {settings.llm_model}")
traces_dir = settings.agent_data_dir / "traces"
trace_count = len(list(traces_dir.glob("trace_*.json"))) if traces_dir.exists() else 0
st.sidebar.markdown(f"**Traces**: {trace_count}")


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def _load_traces() -> list[dict]:
    return load_all_traces(limit=200)

def _load_benchmarks() -> dict[str, Any]:
    bm_dir = settings.agent_data_dir / "benchmark"
    if not bm_dir.exists():
        return {}
    files = sorted(bm_dir.glob("benchmark_*.json"), reverse=True)
    results: dict[str, Any] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            date_label = f.stem.replace("benchmark_", "")
            results[date_label] = data
        except Exception:
            pass
    return results


# ============================================================================
# 页面 1: 概览
# ============================================================================

if page == "📊 概览":
    st.title("📊 Agent 运行概览")

    traces = _load_traces()
    stats = get_trace_stats(traces)

    if stats.get("total", 0) == 0:
        st.info("暂无 trace 数据。运行几次 Agent 调用后会自动生成。")
        st.stop()

    # KPI
    c1, c2, c3, c4, c5 = columns(5)
    with c1:
        metric("总运行次数", stats["total"])
    with c2:
        metric("成功率", f"{stats['success_rate']:.0f}%",
               delta=None if stats['success_rate'] >= 80 else "⬇ 偏低")
    with c3:
        metric("平均延迟", f"{stats['avg_latency_ms']:.0f}ms")
    with c4:
        metric("工具调用", stats["total_tool_calls"])
    with c5:
        metric("记忆更新", stats["total_memory_updates"])

    divider()

    # 按类型分布
    c_left, c_right = columns(2)
    with c_left:
        st.subheader("📂 任务类型分布")
        by_type = stats.get("by_type", {})
        if by_type:
            chart_data = {"类型": list(by_type.keys()), "次数": list(by_type.values())}
            st.bar_chart(chart_data, x="类型", y="次数")
        else:
            st.caption("无数据")

    with c_right:
        st.subheader("🔧 高频工具")
        by_tool = stats.get("by_tool", {})
        if by_tool:
            chart_data = {"工具": list(by_tool.keys()), "调用": list(by_tool.values())}
            st.bar_chart(chart_data, x="工具", y="调用")
        else:
            st.caption("无数据")

    divider()

    # 最新 traces
    st.subheader("🕐 最新运行")
    for t in traces[:5]:
        bg = "🟢" if t.get("success") else "🔴"
        ts = t.get("timestamp", "")[11:19]
        intent = t.get("user_intent", t.get("task_type", "?"))[:50]
        tools = [tc.get("name", "?") for tc in t.get("tool_calls", [])]
        lat = t.get("latency_ms", 0)
        with st.expander(f"{bg} [{ts}] {intent}  ({lat}ms | {len(tools)} 次工具调用)"):
            st.json(t)


# ============================================================================
# 页面 2: Trace 列表
# ============================================================================

elif page == "📋 Trace 列表":
    st.title("📋 Trace 详情")

    traces = _load_traces()
    if not traces:
        st.info("暂无 trace 记录。")
        st.stop()

    # 过滤器
    c1, c2 = columns(2)
    with c1:
        filter_type = st.selectbox(
            "任务类型筛选",
            ["全部"] + sorted({t.get("task_type", "?") for t in traces}),
        )
    with c2:
        search = st.text_input("搜索 intent", "")

    filtered = traces
    if filter_type != "全部":
        filtered = [t for t in filtered if t.get("task_type") == filter_type]
    if search:
        filtered = [t for t in filtered if search.lower() in json.dumps(t).lower()]

    st.caption(f"共 {len(filtered)} 条")
    divider()

    for t in filtered:
        ts = t.get("timestamp", "?")
        intent = t.get("user_intent", t.get("task_type", "?"))
        success = t.get("success", False)
        lat = t.get("latency_ms", 0)
        tok = t.get("total_tokens", 0)
        tools = t.get("tool_calls", [])
        mems = t.get("memory_updates", [])
        notes = t.get("referenced_notes", [])

        with st.expander(
            f"{'🟢' if success else '🔴'} {ts[11:19]} | {intent[:60]} | "
            f"⚡{lat}ms | 🛠{len(tools)}次 | 💾{len(mems)}次记忆"
        ):
            c_info, c_tools, c_mem = columns([2, 2, 1])

            with c_info:
                st.markdown("**基本信息**")
                st.write(f"Trace ID: `{t.get('trace_id', '?')}`")
                st.write(f"时间: {ts}")
                st.write(f"类型: {t.get('task_type', '?')}")
                st.write(f"延迟: {lat}ms")
                st.write(f"Token: {tok}")
                st.write(f"成功: {'✅' if success else '❌'}")

                ctx = t.get("context_sources", [])
                if ctx:
                    st.markdown("**上下文来源**")
                    st.dataframe(ctx, use_container_width=True)

                if notes:
                    st.markdown("**引用笔记**")
                    for n in notes:
                        st.write(f"- {n}")

            with c_tools:
                st.markdown("**工具调用**")
                if tools:
                    for tc in tools:
                        c = "🟢" if tc.get("success") else "🔴"
                        st.write(f"{c} **{tc.get('name', '?')}**")
                        st.caption(f"参数: {str(tc.get('params', {}))[:150]}")
                        st.caption(f"延迟: {tc.get('latency_ms', 0)}ms")
                        if tc.get("error"):
                            st.error(tc["error"][:100])
                else:
                    st.caption("无工具调用")

            with c_mem:
                st.markdown("**记忆更新**")
                if mems:
                    for m in mems:
                        st.write(f"- [{m.get('type', '?')}] {m.get('preview', '')[:50]}")
                else:
                    st.caption("无更新")


# ============================================================================
# 页面 3: 回归评测
# ============================================================================

elif page == "📈 回归评测":
    st.title("📈 回归评测 & 基准测试")

    c1, c2 = columns([1, 2])
    with c1:
        if st.button("▶️ 运行基准测试 (6 个用例)", type="primary", use_container_width=True):
            with st.spinner("运行测试套件..."):
                report = run_benchmark_suite()
            st.success(f"完成! 通过率 {report['pass_rate']}%")
            st.rerun()

    with c2:
        benchmarks = _load_benchmarks()
        if benchmarks:
            st.caption(f"共 {len(benchmarks)} 次基准测试记录")

    divider()

    benchmarks = _load_benchmarks()
    if not benchmarks:
        st.info("尚未运行基准测试。点击上方按钮运行。")
        st.stop()

    # 历史趋势
    dates = sorted(benchmarks.keys())
    st.subheader("📉 历史趋势")

    trend_data = []
    for d in dates:
        r = benchmarks[d]
        trend_data.append({
            "date": d[:10],
            "pass_rate": r.get("pass_rate", 0),
            "avg_latency_ms": r.get("avg_latency_ms", 0),
        })

    if trend_data:
        import pandas as pd
        df = pd.DataFrame(trend_data)
        c1, c2 = columns(2)
        with c1:
            st.line_chart(df, x="date", y="pass_rate", color="#00ff00")
        with c2:
            st.line_chart(df, x="date", y="avg_latency_ms", color="#ffaa00")

    # 最新一次详情
    latest_date = dates[-1]
    latest = benchmarks[latest_date]
    st.subheader(f"📋 最新测试: {latest_date}")

    c1, c2, c3, c4 = columns(4)
    with c1:
        metric("通过率", f"{latest['pass_rate']}%")
    with c2:
        metric("用例数", latest["total_cases"])
    with c3:
        metric("平均延迟", f"{latest['avg_latency_ms']}ms")
    with c4:
        metric("平均 Token", latest.get("avg_tokens", 0))

    for r in latest.get("results", []):
        icon = "🟢" if r.get("success") else "🔴"
        st.write(f"{icon} **{r.get('intent', '?')}**")
        st.caption(f"  {r.get('latency_ms', 0)}ms | `{r.get('trace_id', '')}`")
        st.caption(f"  {r.get('input', '')}")
        divider()


# ============================================================================
# 页面 4: 自评测
# ============================================================================

elif page == "🛠️ 自评测":
    st.title("🛠️ Agent 自评测")

    if st.button("▶️ 运行自评测", type="primary"):
        with st.spinner("运行中..."):
            report = run_self_eval()
        st.success(f"综合评分: {report['overall_score']}/100")

        # 评分
        st.subheader("📊 综合评分")
        st.progress(report["overall_score"] / 100)
        st.metric("总分", f"{report['overall_score']}/100")

        # 各维度
        sections = [
            ("📁 Vault 健康", report.get("vault", {})),
            ("🧠 记忆丰富度", report.get("memory", {})),
            ("🔍 Trace 活跃度", report.get("traces", {})),
            ("🛠️ 工具生态", report.get("tools", {})),
            ("📐 上下文质量", report.get("context_quality", {})),
        ]
        for title, data in sections:
            status = data.get("status", "❌")
            with st.expander(f"{status} {title}"):
                for k, v in data.items():
                    if k == "status":
                        continue
                    if isinstance(v, dict):
                        st.markdown(f"**{k}**")
                        st.json(v)
                    elif isinstance(v, list) and len(v) > 10:
                        st.write(f"{k}: {v[:10]}... (+{len(v)-10})")
                    else:
                        st.write(f"{k}: {v}")

        # 问题列表
        issues = report.get("issues", [])
        if issues:
            st.subheader("⚠️ 待改进")
            for issue in issues:
                st.warning(issue)
        else:
            st.success("✅ 一切正常")
    else:
        st.info("点击按钮运行自评测")


# ============================================================================
# 页面 5: 设置
# ============================================================================

else:
    st.title("⚙️ 设置")

    st.markdown("**环境信息**")
    st.json({
        "vault": settings.obsidian_vault,
        "agent_data": str(settings.agent_data_dir),
        "model": settings.llm_model,
        "provider": settings.llm_provider,
    })

    st.divider()
    st.markdown("**数据目录**")
    st.code(str(settings.agent_data_dir))
    st.caption("traces/ — 运行轨迹 / benchmark/ — 回归测试 / eval/ — 自评测")

    if st.button("🗑️ 清空 Trace", type="secondary"):
        for f in Path(settings.agent_data_dir / "traces").glob("*.json"):
            f.unlink()
        st.success("已清空")
        st.rerun()
