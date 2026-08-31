# Handoff — 三层自进化：L1/L2 已完成，接手 L3（动态工具）

> **交接对象**：继续实现 L3 的开发者 / Agent
> **交接时间**：2026-08-31（commit `830aa46` feat: 三层自进化闭环 — trace 蒸馏 → 策略评估 → 上下文注入 (L1/L2) (#13)）
> **目标**：在 L1/L2 闭环之上实现 L3——Agent 发现工具不足时能自己写工具并立即使用

---

## 1. 一句话背景

本 Agent（`D:\MyAgent`，LangGraph 个人知识 Agent，路由 chatbot/plan/reflect/memory）要实现三层自进化：

- **L1 memory**：经验积累（trace → 蒸馏 → lessons/episodic）✅ **已完成**
- **L2 policy**：方法改进（经验对比 → policies → 连续有效固化 skill / 恶化回滚）✅ **已完成**
- **L3 tool**：自写工具（缺工具信号 → LLM 写 Python → 注册 → 立即生效）⬜ **待实现（本 handoff 的重点）**

核心闭环：`trace 采集(原有) → 蒸馏(L1) → 策略应用(L2) → 注入上下文 → 行为改变 → 新 trace 度量`。

---

## 2. 已完成的 L1/L2（接手前先跑通验证）

### 2.1 新增文件 `app/agent/evolution/`

| 文件 | 职责 | 关键接口/常量 |
|---|---|---|
| `experience.py` | trace 规范化 + 聚合 + **窗口对比** + 蒸馏游标 | `load_traces(limit)` `compare_windows(traces, task_type)` `get_undistilled()` `load_state()/save_state()`；常量 `MIN_DISTILL_TRACES=5` `MAX_DISTILL_INPUT=12` `AUTO_EVOLVE_MIN_INTERVAL_H=12` `RECENT_WINDOW=5` `PREVIOUS_WINDOW=5` `MIN_SAMPLES=6` |
| `distill.py` | L1 蒸馏：一次 LLM 调用产出 experiences + policy_suggestions | `distill_once(force=False, dry_run=False)`；经验写 `lessons.md`/`decisions.md` + episodic + MEMORY.md 索引 |
| `update.py` | L2 应用：策略落库/评分/固化/回滚 + 上下文注入块 | `apply_policy_suggestions()` `evaluate_policies()` `_promote_to_skill()` `build_evolution_block(task, max_chars=900)`；`PROMOTE_THRESHOLD=3` `RETIRE_THRESHOLD=-2` `MAX_POLICIES=12` |
| `runner.py` | 编排 | `evolve_now(force, dry_run)` `maybe_auto_evolve()`（节流+后台线程）`status()` |

### 2.2 改动文件

| 文件 | 改动 |
|---|---|
| `app/agent/memory_store.py` | `build_context` 新增 **Layer 3.5**（L815 附近）：每轮注入 evolution 块（policies 摘要 + 命中 trigger 的 skill），所有路由共享生效 |
| `app/agent/graphs/orchestrator.py` | `run_orchestrator` 末尾（L718-722）：`maybe_auto_evolve()`，节流 + 后台线程，不阻塞响应 |
| `app/cli.py` | `cmd_evolve()`（L309）+ `_evolve_status()`；命令：`python -m app.cli evolve [status\|--dry-run\|--force]` |
| `tests/conftest.py` | `mock_llm` fixture 额外 mock `evolution.distill.get_chat_model`（防止测试触发真实 LLM） |
| `tests/test_evolution.py` | 6 个测试：分批蒸馏游标 / benchmark 排除 / 策略应用 / promote / retire / 注入 |

### 2.3 验证方式

```powershell
# 手动
$env:PYTHONIOENCODING='utf-8'          # GBK 终端必设，否则 emoji 打印崩溃
python -m app.cli evolve status        # 状态总览
python -m app.cli evolve --dry-run     # 只预览，不调 LLM 不写入
python -m app.cli evolve --force       # 真实跑一轮（调用 LLM，注意成本）

# 自动化（沙箱下 pytest 的 tmp 目录必须指到 workspace 内！见 §5）
$env:TMP='D:\MyAgent\.pytest_tmp'; $env:TEMP='D:\MyAgent\.pytest_tmp'
python -m pytest tests/test_evolution.py tests/test_orchestrator_multi_task.py -q -p no:cacheprovider
```

### 2.4 真实数据现状（agent_data/）

- `traces/`：~187 条，task_type 分布 memory/daily_plan/reflect/chatbot/benchmark/plan
- `evolution/state.json`：蒸馏游标、`distill_count`、`auto_evolve_count`
- `evolution/policies.json` + `memory/policies.md`：已有 1 条 proposed 策略（memory 类"批量写合并"）
- `memory/lessons.md` + MEMORY.md 索引：已有蒸馏经验
- **trace 有两种格式**：`save_trace()` 简版（memory/daily_plan/reflect，无 latency/tokens 指标）和 `TraceRecord.save()` 完整版（chatbot，含 latency/tokens/tool_calls）——`experience._normalize` 已兼容

---

## 3. L3 设计草案（已与业务确认，等你实施）

### 3.1 目标行为

当 Agent 执行任务发现"想做的事没有对应工具"（如：用户要查航班，没有航班 API）时：
`缺工具信号 → 提议新工具（LLM 写 Python + schema）→ 校验 → 注册 → 下一轮对话立即可用`。

### 3.2 现有架构盘点（L3 的地基，已全部存在）

- **`app/tool_registry/registry.py`**：`ToolRegistry` 单例，`register_native(RegisteredTool)` / `list_tools_for_llm()` / `execute(name, params)`（按名运行时查找）。`RegisteredTool` = name/description/schema_/source/risk_level/side_effects/handler。已有审计 `_audit()` + 高风险管理
- **`app/tool_registry/native_tools.py`**：`register_all_native_tools(registry)` 启动注册全部内置工具——**动态工具的启动扫描应挂在这里之后**
- **`app/agent/graphs/tools.py`**：适配层。`get_agent_tools()` 缓存 `_agent_tools_cache`（`reset_agent_tools_cache()` 可清）；`_schema_to_pydantic()` 把 JSON Schema 动态转 Pydantic（动态工具 schema 直接复用）
- **`app/agent/graphs/chatbot_graph.py`**：`call_model_node` **每轮**调 `get_agent_tools()`（L230）；`ToolNode(get_agent_tools())` 构建时绑定
- **`app/agent/graphs/orchestrator.py`**：`build_chatbot_graph()` **每次请求重建**（L577 `_run_chat_task`）→ 这是 L3 能"即时生效"的关键

### 3.3 即时生效机制（为什么不用重启）

```
create_tool 注册进 registry
  → reset_agent_tools_cache()          # 清 LLM 工具列表缓存
  → 下一次请求 build_chatbot_graph()   # 图已重建，ToolNode 含新工具
  → call_model_node 拿到新工具列表     # LLM 可见可调
```

注册后 `registry.execute()` 本身按名查找，无需改动。**唯一注意点**：同一会话内的下一次 LLM 轮次即可看到新工具（因为图每请求重建）；若未来把图改为缓存单例，必须在工具版本变化时重建（建议 registry 加 `_version` 计数器 + `reset_agent_tools_cache` 联动）。

### 3.4 建议实施步骤

1. **持久化**：新建 `agent_data/tools/` 目录，每个动态工具一个 `<name>.py`（纯 Python 函数 `handler(**kwargs) -> str`）+ `<name>.json`（RegisteredTool 字段的序列化：name/description/input_schema/risk_level）
2. **启动加载**：`native_tools.py` 的 `register_all_native_tools` 末尾（或 `tools.get_registry()` 里）扫描 `agent_data/tools/*.json`，`exec` 对应 `.py` 注册
3. **新原生工具 `create_tool`**（注册进 registry，供 LLM 自己调用）：
   - 参数：`name`（小写蛇形，白名单字符）、`description`、`input_schema`（JSON Schema，≤ 5 个参数）、`code`（Python 函数体）
   - 流程：`compile()` 语法校验 → **受限命名空间** `exec`（只暴露白名单辅助：vault/memory 读写、requests 可选——参考 `native_tools.py` 里现有工具的实现风格）→ 写入 `agent_data/tools/` → `register_native` → `reset_agent_tools_cache()` → 返回成功
   - 测试执行：注册后可先自调一次验证（`execute_tool(name, 示例参数)`），失败则删除并反馈
4. **安全边界**（沿用现有体系）：
   - 动态工具默认 `risk_level="high"` → 走 `approval_router`（`registry._audit` 已对 high 写审计文件，`approval_router.get_current_pending_keys()` 已有）；只读纯函数可 `low`
   - `compile()` 校验 + 受限 exec 命名空间；`import` 白名单（`json/math/re/datetime` + 项目内 `app.obsidian.vault`、`app.agent.agent_data_service`）
   - 建议：`create_tool` 本身为 medium 风险（LLM 写代码需人工看一眼），可在 chatbot 系统 prompt 声明"调用 create_tool 前先用 ask_clarification 与用户确认意图"
5. **缺工具信号**（接回 L1 闭环）：
   - 方案 A（推荐，改动小）：在 `distill.py` 的 `DISTILL_PROMPT` 输出 schema 增加 `tool_requests: [{name, description, schema, reason}]`，LLM 从 trace 的失败模式/重复 intent 中识别缺工具；runner 把 tool_requests 转成"待审批的 create_tool 预填"（写 `agent_data/tools/pending/`，用户或后续会话确认后注册）
   - 方案 B：chatbot 图里检测"LLM 输出表达了想调用不存在的工具"（`ToolNode` 返回未知工具错误时记录到 trace 的 failure_codes）
6. **测试**：`tests/test_tool_evolution.py`，用 `monkeypatch` 隔离 `agent_data/tools/` 目录（参照 `tests/test_evolution.py` 的 `evo_env` fixture 模式，**注意 §5 的隔离坑**）；断言：注册 → 缓存重置 → `list_tools_for_llm()` 含新工具 → `execute` 成功 → 非法代码被拒

### 3.5 完成定义（Definition of Done）

- [x] `create_tool` 原生工具可用，非法代码（语法错/黑名单 import/超时）被拒且有明确错误
- [x] 动态工具持久化到 `agent_data/tools/`，**服务重启后自动加载**
- [x] 注册后同一会话下一轮即可被 LLM 调用（缓存/图重建链路验证）
- [x] 缺工具信号接入蒸馏（tool_requests）并走审批
- [x] `tests/test_tool_evolution.py` 全绿，且不污染真实 `agent_data/tools/`
- [x] `python -m app.cli evolve status` 能显示动态工具数量

> **L3 已实施（feat/l3-dynamic-tools 分支）**：核心实现见 `app/tool_registry/dynamic_tools.py`。
> 实施时落地的额外决策（与草案的差异）：
> 1. 代码校验用 AST 静态审查（import 白名单/危险内建/dunder 访问）+ compile 语法检查双层，而非仅 compile；
> 2. smoke 测试（`test_params`）调**原始 handler**（异常穿透暴露缺陷），注册进 registry 的是守护版（超时/异常收敛为错误串，`success` 语义一致）；
> 3. 动态工具执行有 10s 守护线程超时（`DYNAMIC_TOOL_TIMEOUT_S`）；
> 4. 蒸馏 `tool_requests` 每轮至多落 3 条进 `agent_data/tools/pending/`，`create_tool` 落地同名工具时自动核销；
> 5. `registry` 新增 `version` 计数器（动态工具注册递增），为未来图缓存单例化预留重建判定；
> 6. 函数体自动包裹判定基于 AST（"import + def" 组合的 LLM 典型输出必须保持顶层函数，不被当函数体嵌套）。

---

## 4. 代码风格约定（沿用现有）

- 模块 docstring 中文；日志用 `logger.info("tag", step=..., 字段=...)` 结构化格式（见 `app/core/logging.py` 用法）
- 类型标注 `from __future__ import annotations` + `dict[str, Any]`
- 延迟 import 避免循环依赖（项目里大量 `try: from ... except: pass` 模式，照抄即可）
- 阈值类常量放模块顶部大写；不要魔法数字
- 所有新功能都要有 `tests/test_*.py`，fixture 隔离数据路径（monkeypatch），LLM 一律 mock

---

## 5. 已知的坑与教训（务必先读，都是踩过的）

1. **pytest 沙箱**：DSH 沙箱拒绝在系统 Temp 建目录（`PermissionError: ...\Temp\dsh-*\pytest-of-*`）。解决：跑 pytest 前 `$env:TMP='D:\MyAgent\.pytest_tmp'; $env:TEMP='D:\MyAgent\.pytest_tmp'`（并先建目录）。**不是代码 bug，别浪费时间排查**
2. **测试隔离不彻底会污染真实数据**：L1/L2 测试曾把测试用的 Skills 条目写进真实 `agent_data/memory/MEMORY.md`（因为只 patch 了 `update._SKILLS_DIR`，没 patch `topic_memory._MEMORY_DIR/_INDEX_FILE`）。教训：**凡是要被写的东西，路径全量 monkeypatch**（`evo_env` fixture 已示范）。`tests/test_evolution.py` 的 fixture 是 L3 测试的模板
3. **GBK 终端**：Windows 终端打印 emoji/中文会 `UnicodeEncodeError`。用 `$env:PYTHONIOENCODING='utf-8'`；`cli.py main()` 已对 stdout 做 GBK 兼容，但 `python -c` 直接打印会炸
4. **蒸馏游标语义**：`distill_once` 按时间**正序逐批**处理积压（`undistilled[:MAX_DISTILL_INPUT]`），游标推进到**批次内最新**时间戳。不要改成"取最新 N 条"——会永久跳过中间积压（此 bug 已修，见 commit 历史）
5. **orchestrator 测试会写真实 agent_data**：conftest 注释明确"plan 路由测试会写入真实的 agent_data/state"，trace 同理（orchestrator 测试会真实跑 `save_trace`）。测试后 trace 数会涨，属已知副作用
6. **`maybe_auto_evolve` 在测试中会真实触发**：orchestrator 测试跑 `run_orchestrator` 会走到自动触发分支（已靠 `mock_llm` patch distill 缓解：fake LLM 返回非 JSON → 蒸馏失败 → 无副作用）。**L3 若在 runner 里加新步骤，记得同步在 `tests/conftest.py` 的 `mock_llm` 里 patch 掉真实副作用**
7. **LLM 成本**：一次蒸馏 = 1 次 LLM 调用（12 条 trace 摘要）。`--force` 会跳过阈值直接调；自动触发 12h 节流。测试勿用 `--force`
8. **`run_reflect`（用户反思路由）≠ 批量蒸馏**：批量蒸馏直接调 LLM，不走 `reflect_graph.run_reflect`（避免蒸馏产生 trace 的递归计数）

---

## 6. 相关参考（接手者快速定位）

- 三层设计讨论：本会话前两轮（L1/L2 方案确认 + 实施）
- `app/agent/evolution/` 三个模块的 docstring 含设计意图
- L2 度量语义：`compare_windows` 的 verdict 判定在 `experience.py`（信号不抵消才出结论；样本 <6 为 insufficient 不评分）
- 安全/审计体系：`app/tool_registry/registry.py` `_audit()` + `app/agent/approval_router.py`（L3 审批复用）
- 现有原生工具写法模板：`app/tool_registry/native_tools.py` 前 80 行（`_search_vault` 等）

---

## 7. 接手后的第一步建议

1. 先跑通 §2.3 的验证（status / dry-run / 6 个测试），确认 L1/L2 现状与文档一致
2. 读 `app/tool_registry/registry.py` + `app/agent/graphs/tools.py`（各 ~200 行，L3 的全部地基）
3. 按 §3.4 的步骤 1→2→3→6 顺序实施（先做 create_tool 最小可用，再接信号与审批）
4. 完成后跑全量测试 + `evolve status`，更新本文档 §3.5 的 DoD 勾选
