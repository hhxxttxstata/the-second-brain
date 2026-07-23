---
name: langgraph-agent-system
description: "LangGraph Agent system: 5 sub-graphs + orchestrator, deployed 2026-07-22"
metadata:
  type: reference
---

# LangGraph Agent System (v2)

Built on 2026-07-22. Replaces the previous LangGraph-lite setup with 5 full LangGraph StateGraph agents.

## Architecture

```
Orchestrator (supervisor LLM)
  ├── Chatbot Graph   → 一般对话 + 工具调用（搜索、查记忆、查资产）
  ├── Plan Graph      → LLM 驱动的每日计划（gather → plan → reflect → commit）
  ├── Reflect Graph   → 批判式反思（analyze → critique → suggest）
  ├── Memory Graph    → 自主记忆管理（decide → write / skip）
  └── Steward         → 数据资产巡检（现有代码，未改写）
```

## Files
- [LLM factory](app/agent/graphs/llm.py) — `get_chat_model()` for DeepSeek
- [Tools](app/agent/graphs/tools.py) — 7 LangChain tools wrapping Data Service Gateway
- [State](app/agent/graphs/state.py) — `AgentState` with `add_messages`
- [Chatbot](app/agent/graphs/chatbot_graph.py) — `MessagesState` + ToolNode, ReAct loop
- [Plan](app/agent/graphs/plan_graph.py) — `TypedDict` state, LLM plan generation + reflection loop
- [Reflect](app/agent/graphs/reflect_graph.py) — 3-stage: analyze → critique → suggest
- [Memory](app/agent/graphs/memory_graph.py) — decide → write/skip with LLM judgment
- [Orchestrator](app/agent/graphs/orchestrator.py) — supervisor routes to sub-agents
- [Routes](app/api/routes_agent_v2.py) — `/agent/v2/chat`, `/agent/v2/plan`, `/agent/v2/reflect`, `/agent/v2/memory`

## APIs
- `POST /agent/v2/chat` — Orchestrator
- `POST /agent/v2/plan` — LLM Daily Plan
- `POST /agent/v2/reflect` — Critique + Suggestions
- `POST /agent/v2/memory` — Auto memory management

## LLM
- Provider: DeepSeek (deepseek-chat / deepseek-v4-flash)
- Config via `.env`: `LLM_API_KEY`, `LLM_BASE_URL=https://api.deepseek.com/v1`
- Used langchain-openai ChatOpenAI with base_url override

## Key Design
- All graphs use `StateGraph` (not `create_react_agent` — deprecated)
- Tools access Data Service Gateway (not direct DB)
- Each graph has its own `MemorySaver` checkpointer
- Orchestrator auto-routes user input to the correct sub-agent
