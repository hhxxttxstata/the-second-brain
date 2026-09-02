# 🤖 个人知识 Agent — 你的第二大脑

一个以 **Obsidian 笔记为知识库**的个人 AI Agent。你平时在 Obsidian 里记笔记、写日记，Agent 直接读这些文件作为知识资产来回答问题、执行任务，并把对话中值得记住的内容写回你的记忆库——用得越久，它越了解你，知识库也越大。

**不建向量库、不做分块 embedding——直接读文件 + 拼上下文 + 交给 LLM。** 你的笔记就是数据库。

```
你在 Obsidian 写笔记
    ↓
Agent 读 .md 文件 → 拼上下文 → LLM
    ↓
回答 / 写回笔记 / 写入记忆
    ↓
你在 Obsidian 里立刻看到
```

---

## 快速开始

### 1. 配置 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，填三项必填配置（任何 OpenAI 兼容接口均可）：

```env
LLM_API_KEY=你的key
LLM_MODEL=你想用的模型名
LLM_BASE_URL=接口地址

# 你的 Obsidian vault 目录（改成自己的笔记路径）
OBSIDIAN_VAULT=D:/path/to/your/vault
```

> 没有现成 vault 也可以跑：建一个空目录即可，对话/记忆/任务/工具能力不受影响，之后随时把笔记放进来。

### 2. 安装依赖

```bash
cd MyAgent
python -m venv .venv                 # 一次性创建虚拟环境（需 Python 3.12+）
.venv\Scripts\activate               # Windows；Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt      # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. 启动（三选一）

```bash
# A. 终端对话（最简单）
python -X utf8 -m app.chat

# B. Web 界面
streamlit run app/chat_web.py

# C. Docker 一键起 Web + API
cp .env.example .env   # 填好 key 后
docker compose up -d   # 浏览器打开 http://localhost:8501
```

---

## 日常使用

### 跟它说话

对话不需要记命令，直接说人话，Supervisor 会自动路由到对应的子 Agent：

```text
👤 帮我搜一下关于 RAG 的笔记          → 检索你的 vault 并回答
👤 记住我喜欢吃辣                     → 记忆 Agent（含冲突检测）
👤 帮我分析一下秋招准备               → 反思 Agent
👤 plan                              → 读日记，生成今日计划
👤 帮我做一个面经统计表               → 生成 Excel（agent_data/outputs/）
👤 写段代码把这份数据清洗一下         → 受控执行 Python（沙箱 + 超时保护）
```

### 内置命令

| 命令 | 作用 |
|---|---|
| `plan` | 生成今日计划 |
| `status` | 系统状态 |
| `model` | 查看可用模型列表 |
| `model <序号或别名>` | 切换模型（持久化，重启仍生效） |
| `help` / `quit` | 帮助 / 退出 |

### 接入自己的 vault

- **本地运行**：`.env` 里改 `OBSIDIAN_VAULT` 指向你的笔记目录即可，无其他配置
- **Docker 运行**：在 `docker-compose.yml` 把笔记目录挂载进来（`./你的笔记:/app/vault`）
- vault 是双向工作区：Agent 读你的笔记回答问题，也会写回内容（如每日计划写入 `diaries/`），你在 Obsidian 里实时可见

### CLI 与 API

```bash
python -m app.cli ask "问题"         # 单次提问
python -m app.cli plan               # 生成今日计划
python -m app.cli status             # 系统状态
python -m app.cli help               # 全部命令（含评测/自进化）

python -X utf8 -m uvicorn app.main:app --port 8000    # API 服务
curl -X POST http://localhost:8000/agent/v2/chat \
  -H "Content-Type: application/json" -d '{"text":"帮我搜搜关于 RAG 的笔记"}'
```

---

## 模型配置与切换

默认模型在 `.env` 里配置（`LLM_MODEL` + `LLM_BASE_URL`，任何 OpenAI 兼容接口均可）。对话中随时热切换，**不需要重启**：

```text
👤 model              → 列出可用模型（当前 ✅ 标记）
👤 model 2            → 按序号切换
👤 model <别名>       → 按别名切换
```

模型预设清单与别名定义在 [app/agent/model_switch.py](app/agent/model_switch.py)，按同样的格式添加自己的模型即可。切换记录持久化到 `agent_data/model_config.json`，不影响 `.env`。

---

## 自进化：越用越顺手

Agent 会从自己的执行轨迹中学习，不需要你做任何事：

```
对话产生 trace → 蒸馏出经验与策略 → 策略验证有效后固化为 skill
              → 缺工具时经你审批自动写一个新工具 → 注入上下文改变后续行为
```

- **经验沉淀**：重复任务更快的做法、踩过的坑，自动写进 lessons/decisions
- **策略固化**：有效的行为策略经隔离评测（A/B 门）验证后固化为 skill，持续生效
- **动态工具**：发现缺工具 → 生成候选（AST 安全审查 + 冒烟测试）→ 征得你同意后注册
- **元进化**：蒸馏 prompt 与进化参数本身也会基于失败分布自我迭代，劣化自动回退
- **治理**：所有自进化动作记录台账；每次进化前自动快照，可一键回滚

想手动干预时：

```bash
python -m app.cli evolve              # 手动跑一轮进化
python -m app.cli evolve status       # 查看状态与 ROI（固化/退役/A-B 通过率）
python -m app.cli evolve meta         # 查看进化参数与 prompt 版本
python -m app.cli evolve rollback --list / --to <snap_id>   # 查看恢复点 / 回滚
```

---

## 评测体系

内置 4 层评测集（golden 回归 / challenge 高难 / exploratory 探索 / security 安全），确定性 grader 逐条判定，并按公认基准方法论设计（BFCL 工具调用、AgentDojo 提示注入、τ-bench 多轮模拟）：

```bash
python -m app.cli eval                      # golden 回归
python -m app.cli eval --tier security      # 安全攻击用例（提示注入/越权/投毒链）
python -m app.cli eval --all                # 全部层级 + 失败分析
python -m app.cli eval --score              # 多维评分卡（含安全硬门槛与自进化 ROI）
python -m app.cli eval --failure            # 失败分类分析（Failure Taxonomy）
```

每次 push 由 GitHub Actions 自动跑回归（需在 repo secrets 配置 `LLM_API_KEY`）；失败 case 自动捕获进 candidate 池，修复后晋升 golden，形成质量闭环。

---

## 部署细节（Docker）

```bash
cp .env.example .env    # 只需填 LLM_API_KEY 等三项
docker compose up -d
```

| 特性 | 说明 |
|---|---|
| 数据持久化 | `agent_data` 卷挂载，记忆/任务/trace 重启不丢 |
| 健康检查 + 自动重启 | 异常退出自动拉起 |
| 无 vault 模式 | 不挂载笔记也能跑，后续随时挂载 |

Web 界面（`app/chat_web.py`）右侧有实时状态面板：路由 / 延迟 / Token / 工具调用 / 上下文来源，每条回答可点反馈按钮（有用/没用/工具错/记忆错），反馈会回流到评测 candidate 池。

---

## 为什么不建向量库

```
传统 RAG:  笔记 → 分块 → 向量化 → 存库 → 检索 → 拼 prompt
                 误差累计    维护成本高
Obsidian 原生:  笔记 → 直接读文件 → 拼上下文 → LLM
                      零转换      零维护
```

个人知识库（几十到几千篇笔记）完全装得进上下文策略里，省掉整个向量管线，笔记永远是唯一的真实来源（Single Source of Truth）。

---

## 项目结构

```
app/
├── chat.py                  # 终端对话入口
├── chat_web.py              # Streamlit Web 界面
├── cli.py                   # CLI（ask/eval/evolve/plan/...）
├── main.py                  # FastAPI API
├── core/config.py           # 配置（读 .env）
├── obsidian/vault.py        # Obsidian vault 纯文件读写
├── tool_registry/           # 工具注册中心（native 工具 + 动态工具）
└── agent/
    ├── graphs/              # Supervisor 路由 + 4 个子 Agent（LangGraph）
    ├── evolution/           # 自进化闭环（蒸馏/策略/工具/meta/台账/ROI）
    ├── trace.py             # 执行轨迹 + 评测引擎
    ├── scorecard.py         # 多维评分卡
    └── memory_store.py / topic_memory.py   # SQLite 记忆 + 索引式长期记忆
agent_data/                  # 运行数据（自动生成；eval/ 评测集随仓库版本控制）
tests/                       # 230+ 测试，无需 LLM key 即可运行
docs/                        # 设计与交接文档
```
