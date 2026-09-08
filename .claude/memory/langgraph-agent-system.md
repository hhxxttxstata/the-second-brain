---
name: langgraph-agent-system
description: "LangGraph Agent system: orchestrator + 4 sub-graphs, deployed 2026-07-22, updated 2026-09-08"
metadata:
  type: reference
---

# LangGraph Agent System (v2)

Built 2026-07-22 as full LangGraph StateGraph agents; updated 2026-09-08
（planner 结构化输出 + cascade + SSE 流式）。

## Architecture

```
Orchestrator (planner LLM 判意图, 小模型 cascade + 规则短路)
  ├── Chatbot Graph   → 一般对话 + 工具调用（vault 检索、记忆读写、受控代码执行等 25+ 原生工具）
  ├── Plan Graph      → LLM 驱动的每日计划（gather → plan → reflect → commit）
  ├── Reflect Graph   → 批判式反思（analyze → critique → suggest）
  └── Memory Graph    → 自主记忆管理（decide → write / skip）
```

（无 Steward Agent——那是旧宪法 Agentic Data Platform 的残留概念，从未实现。）

## Files
- [LLM factory](app/agent/graphs/llm.py) — `get_chat_model()` for DeepSeek（planner_model 配置项指定判意图小模型）
- [Structured output](app/agent/graphs/structured.py) — `invoke_structured()` 统一 JSON mode 结构化输出
- [Tools](app/agent/graphs/native_tools.py) — 25+ 原生工具（vault / memory / registry 直连，无 Data Service Gateway 概念）
- [Chatbot](app/agent/graphs/chatbot_graph.py) — `MessagesState` + ToolNode, ReAct loop
- [Plan](app/agent/graphs/plan_graph.py) — `TypedDict` state, LLM plan generation + reflection loop
- [Reflect](app/agent/graphs/reflect_graph.py) — 3-stage: analyze → critique → suggest
- [Memory](app/agent/graphs/memory_graph.py) — decide → write/skip with LLM judgment
- [Orchestrator](app/agent/graphs/orchestrator.py) — planner 编排子任务（单意图单任务，多意图分解）+ `stream_orchestrator` 流式
- [Routes](app/api/routes_agent_v2.py) — `/agent/v2/chat`、`/agent/v2/chat/stream`（SSE）、`/agent/v2/plan`、`/agent/v2/reflect`、`/agent/v2/memory`

## LLM
- Provider: DeepSeek (deepseek-chat / deepseek-v4-flash)
- Config via `.env`: `LLM_API_KEY`, `LLM_BASE_URL=https://api.deepseek.com/v1`
- langchain-openai ChatOpenAI with base_url override; planner 默认 deepseek-v4-flash（禁 thinking）

## Key Design
- All graphs use `StateGraph` (not `create_react_agent` — deprecated)
- chatbot 共享 SqliteSaver checkpointer（thread_id = 会话 event_id）；orchestrator 无 checkpointer（每次全量 initial state）
- 记忆持久化：SQLite（memory_store）+ agent_data/memory/ 文件（topic_memory）
