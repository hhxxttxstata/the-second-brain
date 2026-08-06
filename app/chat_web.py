"""
Agent Web 对话界面 — Streamlit 单页应用

特性:
    - 左侧对话区（支持多轮，session 感知）
    - 右侧实时上下文状态面板（路由/工具调用/上下文来源/token）
    - 每条回答下反馈按钮（有用/没用/工具错/记忆错）
    - 底部模型切换器

启动:
    streamlit run app/chat_web.py
"""
from __future__ import annotations

import json
from datetime import datetime

import streamlit as st
from streamlit import columns, divider, metric

from app.agent.session import get_default_session, load_messages, append_exchange
from app.agent.graphs.orchestrator import run_orchestrator
from app.feedback import new_feedback, save_feedback
from app.agent.model_switch import AVAILABLE_MODELS, get_current_model_id, set_current_model
from app.core.config import settings

st.set_page_config(
    page_title="🤖 个人知识 Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------------------------------------------------------------------------
# Session state 初始化
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_trace" not in st.session_state:
    st.session_state.last_trace = None
if "last_route" not in st.session_state:
    st.session_state.last_route = None
if "last_input" not in st.session_state:
    st.session_state.last_input = ""


def _get_session_id() -> str:
    return get_default_session()


def _load_history() -> None:
    """从 SQLite 加载今天的会话历史。"""
    if st.session_state.messages:
        return
    sid = _get_session_id()
    history = load_messages(sid, max_turns=20)
    for m in history:
        role = "user" if m.get("role") == "human" else "assistant"
        st.session_state.messages.append({"role": role, "content": m["content"]})


# ---------------------------------------------------------------------------
# 回答 Agent
# ---------------------------------------------------------------------------

def _ask(text: str) -> None:
    """调用 orchestrator 并记录 trace。"""
    sid = _get_session_id()
    history = load_messages(sid)

    st.session_state.last_input = text
    st.session_state.messages.append({"role": "user", "content": text})

    try:
        r = run_orchestrator(
            input_text=text,
            thread_id=sid,
            conversation=history,
        )
        if r.get("success"):
            result = r.get("result", "")
            route = r.get("route", "?")
            st.session_state.messages.append({"role": "assistant", "content": result})
            st.session_state.last_route = route

            if result:
                append_exchange(sid, text, result)

            # 拿最新 trace 展示上下文状态
            try:
                from app.agent.trace import get_latest_trace
                st.session_state.last_trace = get_latest_trace()
            except Exception:
                pass
        else:
            st.session_state.messages.append({
                "role": "assistant", "content": f"❌ {r.get('error', '处理失败')}",
            })
    except Exception as exc:
        st.session_state.messages.append({"role": "assistant", "content": f"❌ {exc}"})


# ---------------------------------------------------------------------------
# 反馈按钮
# ---------------------------------------------------------------------------

def _submit_feedback(failure_type: str) -> None:
    trace = st.session_state.last_trace
    if not trace:
        st.toast("⚠️ 暂无 trace 可反馈")
        return
    fb = new_feedback(
        trace_id=trace.get("trace_id", "?"),
        failure_type=failure_type,
        input_text=st.session_state.last_input,
        trace_data=trace,
    )
    path = save_feedback(fb)
    st.toast(f"✅ 已保存反馈: {path.split('/')[-1]}")


# ---------------------------------------------------------------------------
# Sidebar — 上下文状态面板
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("## 🤖 上下文状态")

    trace = st.session_state.last_trace
    if trace:
        success = trace.get("success", False)
        st.metric("路由", st.session_state.last_route or "?")
        st.metric("状态", "✅ 成功" if success else "❌ 失败")

        lat = trace.get("latency_ms", 0)
        tok = trace.get("total_tokens", 0)
        c1, c2 = columns(2)
        with c1:
            st.metric("延迟", f"{lat}ms")
        with c2:
            st.metric("Token", tok)

        divider()

        tools = trace.get("tool_calls", [])
        st.markdown(f"**工具调用 ({len(tools)})**")
        for tc in tools:
            icon = "🟢" if tc.get("success") else "🔴"
            st.write(f"{icon} `{tc.get('name', '?')}` ({tc.get('latency_ms', 0)}ms)")
            if tc.get("error"):
                st.caption(f"❌ {tc['error'][:80]}")

        ctx = trace.get("context_sources", [])
        if ctx:
            divider()
            st.markdown(f"**上下文来源 ({len(ctx)})**")
            for s in ctx[:8]:
                st.write(f"- `{s.get('type', '?')}` ({s.get('chars', 0)} chars)")

        mems = trace.get("memory_updates", [])
        if mems:
            divider()
            st.markdown(f"**记忆更新 ({len(mems)})**")
            for m in mems[:5]:
                st.write(f"- [{m.get('type', '?')}] {m.get('preview', '')[:40]}")

        failures = trace.get("failure_codes", [])
        if failures:
            divider()
            st.markdown("**失败码**")
            for fc in failures:
                st.error(f"`{fc}`")
    else:
        st.info("发送一条消息后显示上下文状态")

    divider()
    st.markdown(f"**会话**: {_get_session_id()}")


# ---------------------------------------------------------------------------
# Main — 对话区
# ---------------------------------------------------------------------------

st.title("🤖 个人知识 Agent")
st.caption(f"会话: {_get_session_id()} · 模型: {get_current_model_id()}")

# 加载历史
_load_history()

# 对话历史渲染
for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # 每条 assistant 回答下加反馈按钮（最近一条）
        if msg["role"] == "assistant" and i == len(st.session_state.messages) - 1:
            c1, c2, c3, c4 = columns(4)
            with c1:
                st.button("👍 有用", key=f"fb_useful_{i}",
                          on_click=_submit_feedback, args=("useful",))
            with c2:
                st.button("👎 没用", key=f"fb_useless_{i}",
                          on_click=_submit_feedback, args=("useless",))
            with c3:
                st.button("⚠️ 工具错", key=f"fb_tool_{i}",
                          on_click=_submit_feedback, args=("tool_wrong",))
            with c4:
                st.button("🧠 记忆错", key=f"fb_mem_{i}",
                          on_click=_submit_feedback, args=("memory_wrong",))

# 输入框
prompt = st.chat_input("输入你的问题...（plan / 记忆 / 医学 / 数据分析）")
if prompt:
    _ask(prompt)
    st.rerun()

# 模型切换（底部）
st.divider()
c1, c2 = columns([1, 3])
with c1:
    st.markdown("**模型切换**")
with c2:
    current_id = get_current_model_id()
    labels = {m["id"]: f"{m['label']}" for m in AVAILABLE_MODELS}
    selected = st.selectbox(
        "选择模型",
        list(labels.keys()),
        index=list(labels.keys()).index(current_id) if current_id in labels else 0,
        format_func=lambda x: labels[x],
        label_visibility="collapsed",
    )
    if selected != current_id:
        set_current_model(selected)
        st.toast(f"✅ 已切换: {labels[selected]}")
        st.rerun()
