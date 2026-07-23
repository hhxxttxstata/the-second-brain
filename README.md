# 🤖 个人知识 Agent — Obsidian 原生版

一个以 **Obsidian vault 为唯一数据库**的个人知识 Agent。所有笔记、日记、兴趣、计划……全是 `.md` 文件。

**不是查数据库 → 不是向量检索 → 不是混合搜索 → 直接读文件 + 拼上下文 + 交给 LLM。**

---

## 架构

```
你写 Obsidian 笔记
    ↓
Agent 启动 → 读 .md 文件 → 拼进 128K 上下文 → DeepSeek
    ↓
回答 / 写回新的 .md 文件
    ↓
你在 Obsidian 里立刻看到
```

**没有** SQLite、Qdrant、PostgreSQL、ingestion pipeline、Data Service Gateway。
全部替换为 `app/obsidian/vault.py` —— 一个纯文件的读写层。

---

## 项目结构

```
app/
├── main.py                     # FastAPI 入口
├── chat.py                     # 终端交互式 Chatbot
├── cli.py                      # 单次命令 CLI
├── core/
│   ├── config.py               # 只保留 LLM + vault 路径
│   └── logging.py
├── obsidian/
│   └── vault.py                # 🔑 核心：纯文件读写 Obsidian vault
├── agent/graphs/
│   ├── llm.py                  # DeepSeek 模型工厂
│   ├── tools.py                # LangChain 工具（纯 Obsidian 操作）
│   ├── chatbot_graph.py        # 对话 Agent（带上下文注入）
│   ├── plan_graph.py           # 每日计划 Agent（读日记→LLM→写回）
│   ├── reflect_graph.py        # 反思 Agent
│   ├── memory_graph.py         # 记忆 Agent（自动写 .md）
│   └── orchestrator.py         # 多 Agent 编排
├── api/
│   └── routes_agent_v2.py      # API 入口 (/agent/v2/chat|plan|reflect|memory)
└── integrations/
    └── feishu_bot.py           # 飞书 Bot
```

---

## 启动

### 1. 配置 `.env`

```env
LLM_API_KEY=sk-你的deepseek-key
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1
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
👤 > 记住我喜欢吃辣           → 自动写 .md 到 notes/
👤 > 帮我分析一下秋招准备     → 反思模式
👤 > 最近 GitHub 有什么热门   → 实时数据工具
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

| 工具 | 说明 |
|---|---|
| `search_vault` | 全文搜索 .md 笔记 |
| `read_folder` | 读取整个文件夹 |
| `read_file` | 读取特定文件 |
| `write_note` | **写回 Obsidian vault** |
| `get_user_profile` | 从 claude.md 读取档案 |
| `vault_structure` | 列出 vault 结构 |
| `get_fund_data` | 基金净值（akshare） |
| `get_github_trending` | GitHub 热门仓库 |
| `get_ai_news` | AI 行业动态 |

---

## 三层记忆（全部是 .md 文件）

| 层 | Obsidian 位置 | 说明 |
|---|---|---|
| **Profile** | `claude.md` | 用户档案、背景、目标 |
| **Episodic** | `diaries/*.md` | 每天的日记和反思 |
| **Task** | `diaries/*-plan.md` | 每日计划（plan 命令生成） |

---

## 为什么这样做

传统 RAG 架构：

```
笔记 → 分块 → 向量化 → 存数据库 → 检索 → 拼 prompt
      误差累计       维护成本高   质量难控
```

Obsidian 原生架构：

```
笔记 → 直接读文件 → 拼进 128K 上下文 → LLM 理解
      零误差       零维护              模型能力决定上限
```

你的笔记就是数据库。Agent 只做**上下文工程**和**提示词工程**。

---

## 飞书 Bot 接入

需要配置 `.env` 中的飞书凭证，并使用 ngrok/localtunnel 暴露公网地址：

```
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
```

然后在飞书开放平台配置回调 URL：
```
https://你的域名/feishu/webhook
```
