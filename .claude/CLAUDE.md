# CLAUDE.md

## Mission
Personal Knowledge Agent（Obsidian-native）：以用户的 Obsidian vault 为知识源，
直接读文件拼上下文交给 LLM——不建向量库、不做分块 embedding。笔记是唯一的真实来源。

## Core Principle
上下文来自真实文件与真实记忆，不来自编造。每个回答可溯源（来源引用 + trace），
每次执行可复盘（trace/评测），每个行为改变可验证（candidate → 回归 → 晋升）。

## Stack
Python 3.12+, FastAPI, LangGraph, langchain-openai (ChatOpenAI → DeepSeek), SQLite,
Pydantic v2, structlog, Docker; 前端 Vite + React + TS + Tailwind。

## Directory（与磁盘一致）
- app/agent/graphs/   — orchestrator + chatbot/plan/reflect/memory 四个子图 + structured.py
- app/agent/evolution/ — 自进化闭环（distill / update / ab_gate / meta / ledger / roi / experience）
- app/agent/          — trace.py（轨迹+评测引擎）、memory_store.py（SQLite 记忆）、
                        topic_memory.py（MEMORY.md 索引式长期记忆）、scorecard.py、model_switch.py
- app/api/            — routes_agent_v2.py（/agent/v2/*）、routes_workspace.py（/workspace/*）
- app/core/           — config.py（.env settings）、logging.py
- app/obsidian/       — vault.py（vault 纯文件读取，只读）
- app/tool_registry/  — 工具注册中心（native_tools + 动态工具 AST 审查）
- frontend/           — Workspace 三栏工作台（SSE 增量渲染）
- agent_data/         — 运行数据：traces/、memory/、eval/（评测集入 git）、evolution/、outputs/
- tests/              — 230+ 测试，FakeModel mock LLM，无需 API key

## Required APIs（全部真实存在，见 app/api/）
- GET  /health
- POST /agent/v2/chat            — Orchestrator（一次性）
- POST /agent/v2/chat/stream     — Orchestrator 流式（SSE：planner_done → step* → token* → final）
- POST /agent/v2/plan | /reflect | /memory
- GET  /agent/v2/tools | /tools/stats | /tools/audit | /tools/status | /self-eval | /traces
- POST /agent/v2/benchmark
- /workspace/* — summary / runs/{id} / feedback / sessions

## Agent Loop（orchestrator.py）
Planner（结构化输出，小模型判意图）→ Task Graph（4 子 Agent，stage 并行）→ State Merge。
- planner 输出走 `with_structured_output` JSON mode（structured.py 统一封装）；
  禁止新增"prompt 要 JSON + regex 剥壳 + json.loads"式手搓解析。
- cascade：规则短路（纯问候/显式记忆指令，settings.planner_shortcut 可关）
  → planner 小模型（settings.planner_model，默认 deepseek-v4-flash）→ 回答用当前全局大模型。
- 单意图 → 恰好 1 个任务；多意图 → 写类任务在前、读类在后，stage 控制并行。

## Context
vault 只读（obsidian/vault.py）+ 记忆（SQLite 三层：stable_profile / episodic / task）
+ MEMORY.md 索引每轮注入 + 历史压力压缩（context_pressure.py）。
设计哲学就是"直接拼上下文"——不要引入向量库/embedding，除非用户明确要求。

## Memory
- Stable Profile = 用户主数据；Episodic = 事件；Task = 待办/流程（memory_store.py）
- Topic Memory：MEMORY.md 只存指针，详情在 topic 文件（topic_memory.py）
- 可信度纪律：蒸馏写入 lessons/decisions 前校验 provenance trace 存在（distill.py）；
  MEMORY.md 悬空指针由启动巡检 prune_dangling_index_entries 清理；
  trace 滚动清理必须写 cleanup_log.jsonl 审计。
- 治理原则：**文档与记忆里不写代码库里不存在的东西**。记忆/教训/决策必须可溯源。

## Tool Policy
每个工具注册 name / schema / description / risk_level（tool_registry/registry.py）。
执行前校验参数；执行后记录 latency/errors（_audit）。高危动作必须经
approval_router 审批。动态生成的工具必须过 AST 安全审查 + 冒烟测试 + 用户确认。

## Evolution（自进化 = eval-driven controlled evolution）
trace → 蒸馏（distill.py，经验/策略建议）→ 策略评分（update.py）→ A/B 晋升门
（ab_gate.py，隔离子进程）→ skill 固化 / 退役。所有动作记台账（ledger.jsonl），
进化前自动快照、可回滚（ledger.py）。meta 层自迭代蒸馏 prompt，劣化自动回退。
红线：A/B 门 FAIL 的策略不得晋升；评测隔离目录（isolate_agent_data）不得污染生产数据。

## Evaluation
四层评测集：golden 回归 / challenge / exploratory / security（agent_data/eval/）。
确定性 grader 逐条判定（trace.py run_benchmark_suite）；失败自动捕获进 candidate 池，
修复后晋升 golden。CI（golden-regression.yml）每次 push 跑 fast-suite。

## Coding Rules
Pydantic schema + 类型标注；路由薄、逻辑下沉；显式错误处理（子任务失败不中断编排）。
LLM 结构化输出一律走 structured.py。禁止硬编码 secret（key 走 .env）。
不硬编码模型名（用 model_switch 动态取）。
文档（README/CLAUDE.md/handoff/docs）改动必须与代码同一次提交，断言必须可对应到文件。

## Definition of Done
功能可用 + 单测通过（pytest，无需 key）+ golden 回归不回退 + trace 如实记录 +
文档与实际行为一致。吹过的牛必须有代码兜底。

## Avoid
No 未溯源的回答。No 无 trace 的执行。No 手搓 LLM JSON 解析。
No prompt stuffing 之外的"幻觉上下文"。No 记忆/文档描述不存在的机制。
No 绕过审批执行高危工具。No agent_data 运行数据进 git（eval/ 评测集除外）。
