# CLAUDE.md

## Mission
Build **Agentic Data Platform**: a data-middle-platform-backed personal knowledge Agent.
Do not build a simple chatbot. Build governed data services first, then Agents.
做harness engineering来引导LLM，而不是限制LLM！

## Core Principle
Enterprise Agents depend on data management: ingestion, assets, metadata, quality,
lineage, permissions, context service, traces, and feedback loops.

## Stack
Python 3.11+, FastAPI, PostgreSQL, Qdrant/Milvus, Redis, LangGraph, Dify, Docker,
OpenTelemetry, structlog.

## Layers
1. Sources: GitHub, market data, documents, notes, feedback, Agent logs.
2. Ingestion: collectors, parsers, cleaners, deduplication, scheduler.
3. Assets: raw/clean data, chunks, memories, tasks, signals, traces.
4. Governance: metadata, quality, lineage, tags, permissions, audit.
5. Services: asset/search/context/memory/signal/task/report APIs.
6. Apps: daily brief, knowledge QA, daily plan, data steward.

## Directory
Use: app/api, app/assets, app/governance, app/ingestion, app/context, app/memory,
app/agent, app/tools, app/observability, app/db, tests, scripts, dify.

## Build Order
1. Schemas: DataAsset, Metadata, Quality, Lineage, AgentTrace.
2. PostgreSQL persistence and migrations.
3. Asset registration and search APIs.
4. GitHub ingestion.
5. Document ingestion.
6. Context service v1.
7. Daily brief Agent.
8. Daily plan Agent.
9. LangGraph loop.
10. Dify Workflow/Chatflow integration.

## Core Models
DataAsset: id, name, type, source, owner, tags, version, quality, sensitivity, timestamps.
Metadata: asset_id, source_uri, job_id, parser, cleaner, embedding, chunking, upstream, downstream, expires_at.
Quality: asset_id, completeness, freshness, uniqueness, reliability, schema_validity, score, issues.
Lineage: lineage_id, run_id, output_asset_id, input_asset_ids, tools_used, transformations.
Trace: run_id, task_type, context_assets, tool_calls, output, latency_ms, success, error_type.

## Required APIs
POST /assets/register, GET /assets, GET /assets/{id}, GET /metadata/{id},
GET /quality/{id}, GET /lineage/{id}, POST /ingestion/github,
POST /ingestion/document, POST /data-service/search, POST /data-service/context,
POST /agent/daily-brief, POST /agent/daily-plan.

## Context Service
Context must come from data assets, not prompt stuffing.
Rank by relevance, quality_score, freshness, importance, confidence.
Filter by permission, sensitivity, deduplication, and token budget.
Return asset_id, source, quality_score, and selection reason.

## Memory
Treat memory as assets:
- Stable Profile = user master data.
- Episodic Memory = event data.
- Task Memory = workflow/process data.
Fields: importance, confidence, source_text, last_used_at.
Stable Profile updates require user confirmation.

## Agent Loop
Use LangGraph only after Data Service works:
Observe -> Context Build -> Plan -> Tool Use -> Verify -> Reflect -> Memory Commit.

## Tool Policy
Register every tool with name, schemas, description, risk_level.
Before execution: validate params and run Policy Guard.
After execution: log result, latency, errors, and related assets.
High-impact actions require approval.

## Governance
Every ingestion creates asset + metadata + quality record.
Every Agent output creates lineage + trace.
Agents must not bypass Data Service APIs.
Low-quality or expired assets must be filtered, downgraded, or flagged.

## Data Steward Agent
After MVP, detect expired assets, duplicates, low-quality chunks, unused memories,
failed jobs, missing lineage, and outputs without sources.

## Coding Rules
Use Pydantic schemas, typed service classes, thin route handlers, explicit errors.
Do not hardcode secrets. Keep modules small and testable.
Test schemas, services, quality scoring, lineage, and context building.

## Definition of Done
API works, validation works, persistence works, trace/lineage is recorded,
tests pass, and README usage is updated.

## Avoid
No pure chatbot. No untraceable outputs. No prompt-only governance.
No direct storage access from Agents. No long-term profile mutation without confirmation.
