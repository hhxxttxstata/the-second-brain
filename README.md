# 🤖 个人知识 Agent — 你的第二大脑

一个以 **Obsidian vault 为知识库 + SQLite 为运行数据**的个人知识 Agent。obsidian天然以Markdown格式存储文件，你可以在obsidian记录下所有想记录的内容，这个agent会将这些内容作为知识资产，所有你就有了一个“自增长的知识库+十分了解你的专属助手agent”。

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
│   ├── registry.py             # 工具注册中心（统一 schema + 审计 + 风险分级）
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

### 2. 安装依赖（项目级 venv）

```bash
cd D:\MyAgent
python -m venv .venv                          # 创建项目虚拟环境（一次性）
.venv\Scripts\activate                        # 激活（Windows）
# 或 Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt               # 安装依赖（国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
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
👤 > model 2                  → 切换 LLM 模型（model list 查看）
```

### 模型切换

```bash
👤 > model                    # 查看可用模型列表（当前 ✅ 标记）
👤 > model 2                  # 按序号切换（1=DeepSeek V4 Flash 默认）
👤 > model reasoner           # 按别名/ID 切换（reasoner / gpt / claude / chat / flash）
```

可用模型预设（[app/agent/model_switch.py](app/agent/model_switch.py) 可扩展）：
| 模型 | 说明 |
|---|---|
| DeepSeek V4 Flash | 默认，快（关闭思考模式） |
| DeepSeek Chat | 通用 |
| DeepSeek Reasoner | 推理强，慢（temperature 0.3） |
| GPT-4o-mini | OpenAI（需 OPENAI_API_KEY） |
| Claude Sonnet 4.5 | Anthropic（需 ANTHROPIC_API_KEY） |

切换持久化到 `agent_data/model_config.json`，重启后仍生效；不影响 `.env` 配置。

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
| `generate_excel` | 生成 Excel 报表（openpyxl，含表头/自动列宽） |
| `control_visio` | 控制 Visio 画流程图（COM 自动化） |
| `run_code` | 执行 Python 代码（受控目录 + 超时保护） |

### 编程能力（输出不限于 Obsidian）

```bash
👤 > 帮我做一个秋招面经统计表            → generate_excel → agent_data/outputs/*.xlsx
👤 > 画一个秋招准备的流程图              → control_visio → Visio 打开 .vsdx
👤 > 写代码把这段数据处理一下            → run_code → 受控执行返回结果
```

- **Excel**：openpyxl 生成 `.xlsx`，支持表头/多行/自动列宽，输出到 `agent_data/outputs/`
- **Visio**：pywin32 COM 自动化，创建流程图（矩形/菱形/椭圆/平行四边形）+ 导出 PDF
- **代码执行**：受控环境（`agent_data/code_runs/`）+ 超时保护（默认 60s），防死循环

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

## Web 界面（本地 / 云部署）

### 本地 Web 对话界面

```bash
streamlit run app/chat_web.py
# 浏览器打开 http://localhost:8501
```

特性：
- 左侧对话区（session 感知，跨会话延续）
- 右侧**上下文状态面板**（路由 / 延迟 / Token / 工具调用 / 上下文来源 / 失败码）
- 每条回答下**反馈按钮**（有用/没用/工具错/记忆错 → 写入 feedback/）
- 底部**模型切换器**（同 `/model` 命令）

### 云部署（Render / Railway）

```bash
# 1. 本地构建验证
docker build -t agent .

# 2. Render 部署（render.yaml 已配置）
#    - 环境变量: LLM_API_KEY（必须）、LLM_MODEL、OBSIDIAN_VAULT（云上路径）
#    - AGENT_DATA_DIR 可选（默认 /app/agent_data）
#    - 启动命令: python app/cloud_bootstrap.py && streamlit run app/chat_web.py
```

**无 vault 模式**：云上不挂本地 Obsidian 笔记时，`cloud_bootstrap.py` 自动初始化空 vault + agent_data 目录，Agent 保留对话/记忆/任务/工具能力（笔记检索后续可接 git 同步）。

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
