# Handoff — 评测体系统一为 Evaluation Lifecycle（方案对比 + 采纳清单）

> **交接对象**：实施评测体系升级的开发者 / Agent
> **交接时间**：2026-08-31（实施完成，commit 待打）
> **来源方案**：`D:\personal_agent_eval_handoff.md`（通用模板：六层 Evaluation Stack + Workflow 评测升级）
> **本文件用途**：把来源方案与 `D:\MyAgent` 项目**逐项实证对比**，保留合理可行的操作，剔除过时/重复/不可行的部分，形成最终实施清单。
> **状态**：§3 采纳清单 **P0 全部 + P1 + P2 已实施完成**（见 §3 各条 ✅），自进化评测见独立章节 §7。

---

## 7. 自进化评测（本项目特有，来源方案未覆盖，已实施）

> 自进化（L1 蒸馏 / L2 策略 / L3 工具）已上线后，评测体系必须回答：
> **"自进化是否真的让 agent 更快更熟练成本更低？"** 以及 **"评测数据会不会污染自进化学习库？"**

### 7.1 评测隔离（防污染，安全底线）— ✅ 已实施

- **实现**：`app/agent/trace.py` 新增 `isolate_agent_data()` 上下文管理器，把 agent 全部可写路径（memory.db / traces / checkpoint / topic_memory / evolution / handoff / tasks / session_logs / pending / code_runs / agent_data_service._DATA_DIR）重定向到临时目录；`run_benchmark_suite(isolate=True)` 默认启用。
- **保留真实**：benchmark 报告目录（历史对比基线）、eval 数据集、candidate 回流池、vault（fixture 依赖）。
- **验证**：真实评测前后 `traces/` 计数 delta=0（曾发现 `agent_data_service._DATA_DIR` 泄漏并已修复）。
- **逃逸开关**：`python -m app.cli eval --no-isolate`（仅调试）。
- **检测码**：`EVO_MEMORY_LEAK`（failure_taxonomy，critical）——评测数据混入学习库时标注。

### 7.2 演化前后对比 suite — ✅ 已实施

- **实现**：`app/agent/evolution/eval.py` 的 `run_evolution_suite()`：
  - 阶段 A：隔离环境跑任务集 → 基线指标
  - 阶段 B：注入预设策略（模拟自进化已沉淀）→ 同任务集
  - 判定：成功率（硬门槛 0 容忍）+ latency/tokens 变化 + 策略注入生效检查
- **数据**：`agent_data/eval/evolution/basic_tasks.json`（4 个任务：记忆/检索/任务操作/多意图）
- **入口**：`python -m app.cli eval --tier evolution [--dry-run]`
- **实测结果**（2026-08-31）：成功率 100%→100%，latency 6278ms→4616ms（**-26.5%**），硬门槛 PASS。报告落盘 `agent_data/benchmark/evolution_*.json`
- **L3 自举实证**（2026-08-31 第二批）：任务集加 `evo-005`（"帮我写一个天气工具"）后，阶段 A 真实触发 `create_tool`，阶段 B 直接使用创建的 `get_weather` 工具——**自写工具→即时生效闭环在评测中可观测**（report 的 `tools_phase_a/b` + `create_tool_triggered` 字段）；实测 latency -47.4%
- **注意**：单次对比含 LLM 随机性；多轮取中位数后再下结论。L3 的端到端恶意诱导行为在 exploratory 层观测（`dynamic_tool_safety.json`），确定性安全验证靠单元层 27 例。

### 7.3 自进化评测矩阵（与三层对应）

| 自进化层 | 评测点 | 落点 |
|---|---|---|
| L1 蒸馏 | 游标不跳过不重复、benchmark trace 不参与蒸馏、经验落库正确 | `tests/test_evolution.py`（6 测）✅ |
| L2 策略 | promote/retire 阈值判定、skill 固化、注入块命中 | `tests/test_evolution.py` + `test_eval_lifecycle.py` ✅ |
| L2 效果 | 演化前后 latency/tokens/成功率对比 | §7.2 演化 suite ✅ |
| L3 工具 | 校验拒绝/持久化/重启加载/超时/pending 闭环 | `tests/test_tool_evolution.py`（27 测，L3 提交自带）✅ |
| L3 安全行为 | 恶意诱导下是否识别并拒绝（观测层） | `agent_data/eval/exploratory/dynamic_tool_safety.json`（2 case，不进 CI 硬门槛）✅ |
| L3 自举闭环 | 演化评测中 create_tool 触发 + 新工具被使用 | §7.2 演化 suite：`tools_phase_a/b` + `create_tool_triggered` 字段；实测阶段 A 触发 create_tool、阶段 B 直接使用创建的 get_weather 工具 ✅ |
| 防污染 | 隔离模式 trace 零泄漏 | §7.1 + `test_isolate_redirects_and_restores` ✅ |

---

## 1. 对比结论（先说最重要的三个发现）

来源方案是一份质量不错的**通用模板**，但它基于的**前提假设在本项目已部分过时**——项目在 issue #9（Workflow 级评测）和 issue #10（安全评测）两次迭代中已实现了方案 60% 的 P0/P1 内容。真正缺失的是**动作级指标、state 断言增强、效率门、review 队列、多意图数据补强**，以及方案完全没覆盖的**自进化防污染评测**。

| # | 来源方案的假设 | 实证结果 |
|---|---|---|
| 1 | "Supervisor 只能输出 single next，一条消息只执行一个动作，Graph 在子 Agent 后直接 END"（§2/§17） | ❌ **已过时**。`orchestrator.py` 已是多意图分解（tasks 数组 + stage 并行 + 写先读后，PLANNER_PROMPT L88-145）；regression 已有 `multi-intent-memory-then-plan` case；`tests/test_orchestrator_multi_task.py` 9 个测试覆盖 stage/并行/序列 |
| 2 | "核心评测仍偏向 Router Evaluation"（§0） | ❌ **已过时**。`expected_workflow` 逐步断言 + `state_assert` 快照增量 + `Workflow Completion Rate` / `Multi-intent Completion Rate` 已是一级指标（scorecard Level 1B，权重 8%）；`_compute_intent_completion` 实现意图级完成度 |
| 3 | "当前只有四级评测集，建议增加 safety 和 holdout"（§11） | ❌ **已存在**。`agent_data/eval/` 已有 `security/`（11 个 case：prompt injection 6 + security alignment 5）和 `heldout/`；`test_security_eval.py` 硬门槛 |
| 4 | "Routing Accuracy 应是 L2 子指标而非顶层"（§2/§5） | ✅ **已实现**。scorecard 顶层是 E2E（30%），Routing Accuracy 已是 Level 2 权重 10% |
| 5 | "建议新建 evals/ 目录体系"（§21） | ❌ **不采纳**。项目已有等价结构：`app/agent/trace.py`（评测器）+ `agent_data/eval/`（数据集）+ `tests/`（单测）。重构成本高收益低 |
| 6 | "P1: Supervisor single-route → task plan + pending_actions"（§22） | ❌ **已完成**，不再实施 |
| 7 | "统一 JSONL case schema"（§12） | ⚠️ **部分采纳**：保留现有 JSON + `SCHEMA.md` 格式，只增量加字段（见 §3），不换格式 |
| 8 | 基线"Golden Regression = 24/24" | ✅ 属实（dataset 16 + regression 8）；工具 27 个（"25+"属实）；failure taxonomy 现有 17 类（"15 类"属实） |

**一个真实的软肋（来源方案说对了方向但理由错了）**：架构上多意图已支持，但**评测数据里真正的多意图 case 只有 1 个**（golden dataset 16 个 case 的 expected_workflow 全是 1 步包装、intents=1、state_assert=0）。方案"Required Action Recall"指标的担忧仍然成立——只是问题不在架构，而在**评测数据覆盖面**。

---

## 2. 目标模型：不重建框架，用现有分层映射

来源方案的六层 Evaluation Stack（§3）与项目现有 scorecard 分层**一一对应**，无需新框架：

| 方案六层 | 项目现有落点 | 差距 |
|---|---|---|
| L1 Component | `tests/` 单测（memory_privacy / test_evolution 等）+ 各 graph 模块单测 | 无独立 component 数据集；可选补（P2） |
| L2 Decision/Single-step | scorecard Level 2（Routing 10%）；case 的 `expected_route` | 缺 `required_agents` 集合判定（P0-2） |
| L3 Trajectory/Workflow | scorecard Level 1B（Workflow 8%）；`expected_workflow` + `_check_expected_workflow` | 缺动作级指标 + trajectory_mode（P0-1/P1-1） |
| L4 State/Side-effect | `state_assert` 快照增量（`_check_state_delta`，trace.py L666） | 只支持增量计数，缺 memory_type/conflict/updated_at 断言（P0-3） |
| L5 End-to-End | scorecard Level 1（E2E 30%）；`pass_rate` + `required_outcomes` + forbidden | 缺 call/state/goal 三态分离（P0-4） |
| L6 Safety/Cost/Reliability | scorecard Level Sec（硬门槛 10%）+ Level 2/3；security 11 cases | 缺效率门（p50/p95）+ 部分安全 case（P0-5/P2-2） |

---

## 3. 采纳清单（按实施顺序）

> **实施状态（2026-08-31 全部完成 ✅）**：P0-1~P0-8、P1-1~P1-3、P2-1~P2-3 均已落地并验证。
> 验证：185 个 pytest 全过（含新增 `tests/test_eval_lifecycle.py` 16 测）；真实 golden regression 8/8 通过（隔离模式，trace 零泄漏）；真实演化对比 latency -26.5%。

### P0 — 低成本高价值（✅ 已实施，全部在现有文件内增量改）

**P0-1 动作级指标：Required Action Recall / Unnecessary Action Rate / Premature Stop Rate**（方案 §6.3）
- 现状：`_compute_intent_completion` 是**意图级**完成度；没有"必要动作（工具调用）"级的召回/精确率，也没有多余动作/重复动作/提前结束的检测。
- 改法：`trace.py` 的 `_check_expected_workflow` 已产出 `completed_steps/total_steps`，在 benchmark report 聚合层加：
  - `required_action_recall` = 命中 `expect_tool` 的步骤数 / 含 expect_tool 的步骤总数（已有数据，直接聚合）
  - `unnecessary_action_rate` = 调用次数超出必要步骤数 / 总调用（判定：tool_calls 里不在任何 expect_tool 候选集且不在 forbidden 的调用）
  - `premature_stop_rate` = `_judge_case` 中 run_ok=True 但 workflow 未 complete 的 case 占比（已有字段）
- 涉及：`app/agent/trace.py`（report 聚合 + results 字段）、`scorecard.py`（L1B 加指标）

**P0-2 case schema 增加 `required_agents`**（方案 §5）
- 现状：多意图用 `intents` + `expected_workflow`（intent 字段映射），但没有"本次请求必须覆盖哪些 agent"的集合断言。
- 改法：`SCHEMA.md` 增加选填字段 `required_agents: ["chatbot","reflect","memory","plan"]`；grader 在 `_check_case_constraints` 里加一条 outcome："task_results 中出现的 agent 集合 ⊇ required_agents"（数据已有：`result.get("task_results")[].agent`）。这是最便宜的"多意图不漏"检查。
- 涉及：`agent_data/eval/SCHEMA.md`、`app/agent/trace.py`

**P0-3 state_assertions 增强**（方案 §7）
- 现状：`_check_state_delta`（trace.py L666）只支持增量语法："todo 出现（新增 N 条）" / "episodic 新增 N 条" / "…status=x"。
- 改法：`_check_state_delta` 扩展语法（在快照函数 `_snapshot_state` 补维度）：
  - `"memory.deprecated_new >= 1"`（本次新增记忆且旧冲突记忆被标记 deprecated——验证 conflict invalidation 真发生）
  - `"memory_type=profile changed"`（profile 字段真的变更）
  - `"task.updated_at changed"`（更新的时间戳变化）
- 涉及：`app/agent/trace.py`（`_snapshot_state` + `_check_state_delta`）

**P0-4 三态分离：call_success / state_success / user_goal_success**（方案 §7.3）
- 现状：results 里 `success`（含 outcome 判定）与 `tool_calls[].success` 分开存在，但没有显式三态。
- 改法：`trace.py` 的 results 增加三个布尔字段：`call_success`（工具调用全部成功）、`state_success`（state_assert 全满足）、`user_goal_success`（outcome 全满足 + workflow complete）。report 聚合输出三态不一致率（如 "state_success=False 但 user_goal_success=True" = FALSE_COMPLETION 特征）。
- 涉及：`app/agent/trace.py`

**P0-5 Efficiency 指标细化 + 效率门**（方案 §10.1/§18）
- 现状：report 只有 `avg_latency_ms` / `avg_tokens`；`_regression_guard`（trace.py L328）只对比 pass_rate 一个维度。
- 改法：
  1. report 增加 `latency_p50_ms` / `latency_p95_ms` / `timeout_count`（latency > 60s 计为 timeout）——`run_benchmark_suite` 的 results 已有每 case latency，聚合即可
  2. `_regression_guard` 扩展为多维：`pass_rate_drop`（现有）+ `p95_latency_regression_pct`（>15% 告警）+ `avg_tokens_regression_pct`（>20% 告警）。与自进化 L2 的"更快更省"度量天然合流（见 P2-1）
- 涉及：`app/agent/trace.py`

**P0-6 failure taxonomy 增补三个码**（方案 §13）
- 现状：17 类。方案 23 类列表中大部分已有等价（ROUTING_WRONG_PRIMARY≈ROUTING_ERROR、WRONG_TOOL≈WRONG_TOOL、FALSE_COMPLETION≈FALSE_COMPLETION 等）。
- 改法：新增 3 个并接入 `detect_failure_codes`：
  - `MISSED_SECONDARY_INTENT`（多意图只执行了主意图；自动检测：case 定义了 required_agents/intents 但 task_results 覆盖不全）
  - `PREMATURE_END`（workflow 未 complete 但 run_ok；检测见 P0-1）
  - `SIDE_EFFECT_MISSING`（声称完成但 state_assert 未满足；与 FALSE_COMPLETION 区别：FALSE_COMPLETION 是"没调用工具"，SIDE_EFFECT_MISSING 是"调用了但状态没变"）
- 涉及：`app/agent/failure_taxonomy.py`（ALL_FAILURE_CODES + detect 逻辑）

**P0-7 review_queue 正式化**（方案 §14.3）
- 现状：suspicious_pass 标注 + unknown 不放行已有，但没有人工复核队列。
- 改法：`agent_data/review_queue.jsonl`，`run_benchmark_suite` 把 suspicious_pass / 双 grader 分歧（rules 通过但 LLM judge 不通过，反之亦然）写入队列；`app/cli.py` 加 `eval --review` 列出待复核。人工裁定后更新 rubric 或 expectation。
- 涉及：`app/agent/trace.py`、`app/cli.py`

**P0-8 多意图评测数据补强**（方案 §12 的内容方向；**P0-1/P0-2 的依赖**）
- 现状：golden 16 个 case 全是 1 步 workflow；multi-intent case 仅 regression 1 个。
- 改法：把 `tests/test_orchestrator_multi_task.py` 已验证的场景（并行 stage / 依赖 stage / 写先读后 / 部分失败）沉淀为 3-4 个 challenge case（`agent_data/eval/challenge/multi_intent_workflow.json`），用 `intents` + `expected_workflow` + `required_agents` 定义。这是让动作级指标有数据可评的前提。
- 涉及：`agent_data/eval/challenge/`

### P1 — 中价值（✅ 已实施）

**P1-1 trajectory_mode 显式化**（方案 §6.2/§15）✅
- 实现：`_check_trajectory_mode()` 支持 `unordered_required`（配合 `required_tools`，全出现且成功、顺序不限）+ `scope`（`allowed_tools` 白名单，越权调用即失败）；steps 增加 `kind`/`requires_tool` 标记供动作级指标。缺省仍走现有 ordered 模式，不破坏旧 case。
- 文档：`SCHEMA.md` 2a' 节。
- 现状：`expected_workflow` 的 `ordered` 布尔（默认 true）+ `expect_tool: "A 或 B"`。能表达"顺序"和"任一"，**不能表达"集合必须全出现（无序）"和"工具白名单 scope"**。
- 改法：case 顶层增加选填 `trajectory_mode: "strict" | "ordered_subsequence" | "unordered_required" | "scope"`：
  - `unordered_required`（多意图主模式）：配合 `required_tools: ["reflect", "write_memory", ...]`，判定 = required 全出现且成功、顺序不限、无 forbidden——`_check_expected_workflow` 加一个分支，**不破坏现有 case**（缺省仍走现有 ordered 逻辑）
  - `scope`：`allowed_tools` 白名单，任何白名单外调用 → FAIL（防 excessive agency 的 schema 化；现有 security-excessive-agency 是 prompt 级，这个是数据级）
- 涉及：`agent_data/eval/SCHEMA.md`、`app/agent/trace.py`

**P1-2 L4 工具副作用细化**（方案 §7.3 的 tool 侧）✅（部分）
- 已落地：三态分离 `call_success / state_success / user_goal_success`（P0-4）+ state_assert 扩展（P0-3）覆盖"状态真的被改了吗"。
- 未做：独立 `side_effect_assert` 字段（如 handoff 文件级检查）——现有 `handoff.active_new` 语法已覆盖主要场景，按需再补。

**P1-3 CI 分级**（方案 §19）✅
- 实现：`.github/workflows/golden-regression.yml` 拆两 job：`fast-suite`（PR：pytest + schema 校验，无 LLM）+ `eval`（仅 push 到 master：golden 回归，隔离模式）。nightly/release 分级暂不建（单人项目成本不划算）。

### P2 — 项目特有（来源方案未覆盖，但自进化已上线，必须做）✅ 全部完成，详见 §7

**P2-1 评测环境隔离（防污染，安全底线）** ✅ — `isolate_agent_data()` + `run_benchmark_suite(isolate=True)` 默认启用；实测 trace 零泄漏（delta=0）；`--no-isolate` 逃逸开关。

**P2-2 演化前后对比 suite** ✅ — `app/agent/evolution/eval.py` + `eval --tier evolution`；实测 latency -26.5%、成功率不降。

**P2-3 evo 失败码 + 门禁** ✅ — `EVO_MEMORY_LEAK`（critical）等 4 个新码；`tests/test_evolution.py` + `tests/test_eval_lifecycle.py` 随 CI fast-suite 跑。

---

## 4. 不采纳清单（对比后明确不做）

| 方案条目 | 理由 |
|---|---|
| §21 新建 `evals/` 目录体系（schemas/datasets/graders/runners/baselines） | 项目已有等价结构（`app/agent/trace.py` + `agent_data/eval/` + `tests/`），重构是纯搬迁无收益 |
| §12 统一 JSONL case 格式 | 现有 JSON 数组 + `SCHEMA.md` 已标准化且被 grader/回流/晋升全链路引用；增量加字段即可 |
| §22 P1 Graph 改造（task plan / pending_actions / continue-stop） | 已完成（orchestrator 多任务分解 + stage 并行） |
| §3 六层 stack 作为新框架 | 与现有 scorecard 分层一一对应，只做映射（见 §2 表） |
| §9 Browser MCP / §26 Browser 安全评测 | 项目无 MCP/浏览器能力 |
| §25 外部认证宣称 | 仅作方法对齐参考，不写进简历 |
| §16 的 12 步 promotion 闭环 | 已有等价（candidate 自动回流 `_auto_capture_failures` + `report` 命令 + SCHEMA.md 晋升字段补齐表）；可加 review_queue（P0-7）但不需要重排流程 |
| §4 Component 评测全集（Recall@K 等） | 方向正确但个人项目过重；现有单测 + scorecard 已覆盖主要风险，列为可选不做 |

---

## 5. 实施顺序与验证

```
P0-8 (补多意图数据) → P0-1/P0-2 (动作级指标+required_agents) → P0-3/P0-4 (state 断言+三态)
→ P0-5/P0-6 (效率门+失败码) → P0-7 (review 队列) → P1-1 (trajectory_mode) → P2-1 (隔离) → P2-2 (演化对比)
```

每步验证：
```powershell
$env:TMP='D:\MyAgent\.pytest_tmp'; $env:TEMP='D:\MyAgent\.pytest_tmp'   # 沙箱下 pytest 必须
python -m pytest tests/test_orchestrator_multi_task.py tests/test_evolution.py -q -p no:cacheprovider
python -m app.cli eval --tier golden          # 回归（真实 LLM，注意成本）
python -m app.cli eval --tier evolution       # P2-2 后
```

**Definition of Done（已完成，2026-08-31 实测确认）**：
- ✅ `required_action_recall` / `unnecessary_action_rate` / `premature_stop_rate`（report 聚合字段）
- ✅ `latency_p50_ms` / `latency_p95_ms` / `timeout_count`
- ✅ `regression_guard` 多维（pass_rate + p95 + tokens + workflow）+ `promotion_gate` 汇总
- ✅ results 每条含 `call_success / state_success / user_goal_success` 三态
- ✅ failure taxonomy 21 类（新增 `MISSED_SECONDARY_INTENT / PREMATURE_END / SIDE_EFFECT_MISSING / EVO_MEMORY_LEAK`）
- ✅ `agent_data/review_queue.jsonl`（suspicious_pass / workflow 未完成自动入队，`eval --review` 查看）
- ✅ challenge 集新增 3 个多意图 workflow case（`multi_intent_workflow.json`：并行/依赖/conflict-update）
- ✅ `run_benchmark_suite` 隔离模式下 trace 零泄漏（实测 delta=0）
- ✅ 演化对比 suite 跑通（latency -26.5%，硬门槛 PASS）
- ✅ 额外修复：failure 分布误报 97% bug（简版 trace 无 success 字段被当失败）、memory_graph 输出"已保存"不被确认词识别、任务记忆 case fixture 缺口

---

## 6. 相关参考

- 评测器主体：`app/agent/trace.py`（`run_benchmark_suite` 隔离包装 / `_regression_guard` 多维 / `_check_trajectory_mode` / `_check_state_delta` 扩展 / `isolate_agent_data`）
- 演化评测：`app/agent/evolution/eval.py` + `agent_data/eval/evolution/`
- case 规范：`agent_data/eval/SCHEMA.md`（§2a required_agents / §2a' trajectory_mode / §P0-3 state_assert 扩展）
- 数据分层：`agent_data/eval/{golden,challenge,exploratory,candidate,heldout,security,meta,evolution}/`
- 评分卡：`app/agent/scorecard.py`（Level 1/L1B/Sec 定义在文件头注释）
- 失败码：`app/agent/failure_taxonomy.py`（21 类，含自进化分类）
- CI：`.github/workflows/golden-regression.yml`（fast-suite / eval 分级）
- 多意图架构（方案假设已过时的证据）：`app/agent/graphs/orchestrator.py` PLANNER_PROMPT L88-145
- 自进化现状：`app/agent/evolution/` + `docs/handoff.md`（L3 交接，含自进化评测的 P0 隔离要求）
- 新测试：`tests/test_eval_lifecycle.py`（16 测：trajectory_mode / required_agents / state_assert / 多维 guard / 隔离）
