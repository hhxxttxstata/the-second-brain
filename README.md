# 🤖 个人知识 Agent — Obsidian 原生版

一个以 **Obsidian vault 为知识库 + SQLite 为运行数据**的个人知识 Agent。所有笔记、日记、兴趣、计划……全是 `.md` 文件。

**不是查数据库 → 不是向量检索 → 不是混合搜索 → 直接读文件 + 拼上下文 + 交给 LLM。**

---

## 架构

```
你写 Obsidian 笔记
    ↓
Agent 启动 → 读 .md 文件 → Context Builder 拼上下文 → DeepSeek
    ↓
回答 / 写回新的 .md 文件 / 写入记忆 (SQLite + Topic Files)
    ↓
你在 Obsidian 里立刻看到
```

- **vault/**（`D:/MYWORLD`）— 人类写的知识资产（笔记/日记/习惯），纯文件读写
- **agent_data/** — Agent 运行数据（记忆 SQLite、Topic Files、tasks、traces、eval），版本控制只纳入 `eval/` 评测集
- 无向量数据库、无 ingestion pipeline——Agent 直接读文件 + 上下文工程

---

## 项目结构

```
app/
├── main.py                     # FastAPI 入口
├── chat.py                     # 终端交互式 Chatbot
├── cli.py                      # 单次命令 CLI (ask/eval/plan/report/...)
├── core/
│   ├── config.py               # LLM + vault + 医疗RAG 配置
│   └── logging.py
├── obsidian/
│   └── vault.py                # 🔑 核心：纯文件读写 Obsidian vault
├── tool_registry/
│   ├── registry.py             # 工具注册中心（native + MCP）
│   └── native_tools.py         # 18 个 native 工具（vault/记忆/外部/医学）
├── agent/
│   ├── memory_store.py         # SQLite + FTS5 记忆存储
│   ├── topic_memory.py         # MEMORY.md 索引 + Topic Files 记忆
│   ├── context_pressure.py     # 上下文压力监控 + 自动压缩
│   ├── failure_taxonomy.py     # 失败分类系统（15 种错误码）
│   ├── handoff.py              # 跨会话任务传递 (tasks/ + handoffs/)
│   ├── session.py              # 会话消息持久化 + 自动摘要
│   ├── session_jsonl.py        # 结构化会话转录 + Resume
│   ├── pending_ledger.py       # 审批/幂等账本
│   ├── trace.py                # Trace 记录 + Benchmark 评测
│   ├── scorecard.py            # 27 维评分卡
│   ├── self_eval.py            # 自评测健康检查
│   ├── capture.py              # 一键捕获 badcase
│   └── graphs/
│       ├── llm.py              # DeepSeek 模型工厂
│       ├── tools.py            # LangChain 工具绑定
│       ├── chatbot_graph.py    # 对话 Agent（带上下文注入）
│       ├── plan_graph.py       # 每日计划 Agent
│       ├── reflect_graph.py    # 反思 Agent
│       ├── memory_graph.py     # 记忆 Agent（冲突检测 + 语义提炼）
│       └── orchestrator.py     # Supervisor 多 Agent 路由
└── api/
    └── routes_agent_v2.py      # API 入口 (/agent/v2/chat|plan|reflect|memory)
```

---

## 启动

### 1. 配置 `.env`

```env
LLM_API_KEY=sk-你的deepseek-key
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1

# 医疗 RAG 服务（可选，Pulmonary_embolism_system）
MEDICAL_RAG_URL=http://127.0.0.1:8001
MEDICAL_RAG_API_KEY=
```

### 2. 安装依赖

```bash
cd D:\MyAgent
pip install -r requirements.txt
```

### 3. 交互式对话

```bash
cd D:\MyAgent
python -X utf8 -m app.chat
```

```
👤 > 你好
👤 > plan                     → 读日记 → LLM 生成计划 → 写回 diaries/
👤 > 记住我喜欢吃辣           → 记忆 Agent 写入（含冲突检测）
👤 > 帮我分析一下秋招准备     → 反思模式
👤 > 肺栓塞的CTPA征象有哪些   → 调用医疗 RAG 工具
👤 > status                   → 系统状态
```

### 4. API 服务器

```bash
python -X utf8 -m uvicorn app.main:app --reload --port 8000
```

```bash
curl -X POST http://localhost:8000/agent/v2/plan
curl -X POST http://localhost:8000/agent/v2/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"帮我搜索关于RAG的笔记"}'
```

---

## Agent 能力

### 工具生态（18 个 native 工具）

| 工具 | 说明 |
|---|---|
| `search_vault` / `read_folder` / `read_file` | Obsidian vault 读写 |
| `search_topic_memory` / `read_topic_memory` / `write_topic_memory` | 索引式记忆 |
| `search_memories` / `read_memory` / `write_memory` | SQLite 记忆 |
| `update_task_status` | 任务状态管理 |
| `get_fund_data` | 基金净值 |
| `get_github_trending` | GitHub 热门仓库 |
| `get_ai_news` | AI 行业动态 |
| `medical_rag_query` | 医学知识库问答（桥接医疗 RAG 系统） |
| `medical_pe_diagnosis` | 肺栓塞影像诊断（桥接医疗 RAG 系统） |

### 多 Agent 编排（Supervisor 路由）

| 子 Agent | 职责 |
|---|---|
| **chatbot** | 对话、知识检索、外部工具调用 |
| **plan** | 计划生成、任务操作 |
| **reflect** | 反思分析 |
| **memory** | 记忆写入（冲突检测、语义提炼） |

---

## 记忆系统（三层）

| 层 | 存储 | 说明 |
|---|---|---|
| **Profile** | SQLite + `people/tata.md` | 用户档案、偏好 |
| **Episodic** | SQLite (FTS5) | 事件记忆、对话摘要 |
| **Task** | SQLite + tasks/ | 待办、跨会话任务 |

**索引式记忆**：`MEMORY.md`（~600 chars）每轮注入上下文，详情通过 `read_topic_memory` 按需读取，避免全部记忆倒入 prompt。

---

## 上下文管理

- **Context Builder**：分层注入（系统策略 → 画像 → 记忆索引 → 任务 → 审批 → 会话延续 → 按需检索），token 预算 3500
- **Context Pressure Monitor**：历史消息超预算（4000 tokens）自动压缩——保留最近 6 轮原文 + 早期用会话摘要替代
- **会话持久化**：SQLite 存消息，每 3 轮 LLM 摘要写入记忆，跨会话通过 session JSONL resume

---

## 评测体系（4 层 90+ cases）

```
agent_data/eval/
├── golden/          # 稳定回归（24 cases，100% 通过）
│   ├── regression.json   # badcase 修复后晋升
│   └── dataset.json      # 核心能力用例
├── challenge/       # 高难度（含长会话退化测试）
├── exploratory/     # 模糊/外部依赖用例
├── candidate/       # 真实用户反馈捕获
└── heldout/         # 留出集
```

### 运行评测

```bash
python -m app.cli eval                      # golden regression
python -m app.cli eval --tier golden        # golden 全部
python -m app.cli eval --all                # 所有层级 + 失败分析
python -m app.cli eval --score              # 27 维评分卡
python -m app.cli eval --failure            # Failure Taxonomy 分析
python -m app.cli eval --tier golden --llm  # LLM Grader 深度评判
```

### 4 类 Grader

| Grader | 类型 | 作用 |
|---|---|---|
| Benchmark 规则 | 规则匹配 | required_outcomes / forbidden_actions 逐条判定 |
| 评分卡 V2 (27 维) | 统计溯源 | E2E/RAG/路由/记忆/工具/稳定性/反馈 |
| 自评测 (5 维) | 健康检查 | 启动时自动 |
| LLM Judge | LLM 定性 | `--llm` 可选 |

### Failure Taxonomy

每个失败 trace 自动映射到标准错误码（`ROUTING_ERROR` / `FALSE_COMPLETION` / `MEMORY_RECALL_MISS` / ... 共 15 种），支持分布分析、热力图、代表 trace 追踪。数据闭环：

```
Trace → Failure Code → Candidate Case → 修复 → Regression Case
```

---

## 医疗 RAG 接入（可选）

通过 HTTP 桥接 `Pulmonary_embolism_system`（肺栓塞医学知识库 + 影像推理）：

```bash
# 启动医疗 RAG 服务（另一个项目）
cd D:\Pulmonary_embolism_system
API_PORT=8001 python app.py
# 或 docker compose up（8001:8000）
```

之后 Agent 会自动路由医学问题（肺栓塞/CTPA/血栓/医学文献）到 `medical_rag_query` 工具，从医学知识库检索回答。服务不可达时优雅降级（如实告知，不编造）。

---

## 为什么这样做

传统 RAG 架构：

```
笔记 → 分块 → 向量化 → 存数据库 → 检索 → 拼 prompt
      误差累计       维护成本高   质量难控
```

Obsidian 原生架构：

```
笔记 → 直接读文件 → 拼进上下文 → LLM 理解
      零误差       零维护        模型能力决定上限
```

你的笔记就是数据库。Agent 只做**上下文工程**和**提示词工程**。
