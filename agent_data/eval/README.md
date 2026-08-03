# Agent 评测体系

## 四级结构

| 层级 | 目录 | 特征 | 晋升条件 |
|---|---|---|---|
| 🥇 **golden** | `golden/` | 稳定、定义清晰、每次变更必须过 | 见准入决策树 |
| 🥇→📌 **golden/regression** | `golden/regression.json` | Badcase 修复后稳定通过 | 见升级条件 |
| ⚔️ **challenge** | `challenge/` | 明确定义的高难度用例 | 补充 Grader + 环境后评审 |
| 🔍 **exploratory** | `exploratory/` | 宽泛/模糊用例，发现新失效模式 | 不一定晋升，有发现即可 |
| 📝 **candidate** | `candidate/` | 来自真实用户的原始问题，质量可低 | 直接从反馈创建 |

---

## Golden 准入决策树

```
这道题测的行为明确吗？
   否 → Candidate

初始环境完整、可复现吗？
   否 → Candidate

正确行为是否来自产品规则或客观事实？
   否 → 人工决定

Reference Solution 是否通过全部硬 Grader？
   否 → Candidate

典型错误方案是否会失败？
   否 → 修改 Grader

是否覆盖合理替代方案？
   否 → 补充 Expectations

是否存在未解决的安全/数据边界歧义？
   是 → 人工审核

全部满足
   → Golden Case
```

## Golden → Regression 升级条件

修复后的 Badcase 进入 regression，还需额外满足：

1. **旧版本能复现问题** — fixture 足够稳定复现
2. **新版本多次稳定通过** — 连续 N 次（≥3）全部通过
3. **相邻正反案例没有退化** — regression 其它用例保持通过，challenge 未新增失败

## 用例字段定义

```json
{
  "intent": "简短描述",
  "input": "用户实际输入",
  "expected_route": "chatbot|plan|reflect|memory",
  "stage": "candidate|challenge|exploratory|golden",

  "checks": ["route", "memory_update", "vault_searched", ...],

  "required_outcomes": [
    "路由到 plan",
    "输出包含至少3条计划项"
  ],
  "forbidden_actions": [
    "路由到 chatbot 只做口头回复",
    "丢弃 merge 意图"
  ],

  "fixture_needed": {
    "profile": "需预置用户信息",
    "vault": "需有 RAG 笔记文件",
    "task_memory": "需预置一条 todo",
    "memory": "需有情绪相关的记忆"
  },

  "known_issue": "当前未通过的原因",
  "note": "补充说明",

  "regression_badge": true,
  "badcase_from": "BC5 - 路由错误",
  "ablation_trigger": "某种条件下会退化的描述"
}
```

## 生命周期

```
真实用户发现问题
        │
        ▼
创建 candidate + exploratory
        │
        ▼
补充 fixture + required_outcomes + forbidden_actions
        │
        ▼
检查 Grader 是否公平
        │
        ▼
变成 golden + challenge
        │
        ▼
优化 Agent
        │
        ▼
连续稳定通过 N 次 + 相邻不退化
        │
        ▼
变成 golden + regression
```

- 不是所有 exploratory 都必须晋升
- 大量低价值、重复、定义不清的 candidate **可以直接删除**

## 命令

```bash
agent eval                        # golden/regression.json
agent eval --tier golden          # golden 全部（regression + dataset）
agent eval --tier challenge       # challenge
agent eval --tier exploratory     # exploratory
agent eval --tier candidate       # candidate
agent eval --all                  # 所有层级合并 + 失败分析
agent eval --failure              # 独立运行 Failure Taxonomy 分析
agent eval --tier golden --llm    # 带 LLM Grader 深度评判
agent eval --score                # 多维评分卡 V2（含 Failure Taxonomy）
```

## Grader 实现

| 组件 | 文件 | 作用 |
|---|---|---|
| Benchmark 规则 Grader | `app/agent/trace.py` `run_benchmark_suite()` | 路由匹配 + required_outcomes + forbidden_actions |
| 评分卡 V2 (27 维) | `app/agent/scorecard.py` `run_scorecard()` | 统计溯源分析，含 Failure Taxonomy |
| 自评测 (5 维) | `app/agent/self_eval.py` `run_self_eval()` | 启动时健康检查 |
| LLM Judge (可选) | `app/cli.py` `_grade_with_llm()` | `--llm` 参数启用，定性评判 |
| Failure Taxonomy | `app/agent/failure_taxonomy.py` | 15 种错误码自动检测 + 聚合分析 |

## Failure Taxonomy

每个失败 trace 自动映射到一个标准错误码（见 `app/agent/failure_taxonomy.py`）:

| 错误码 | 类别 | 严重程度 | 检测方式 |
|---|---|---|---|
| `ROUTING_ERROR` | 路由与意图 | high | 期望 vs 实际路由 |
| `MISSING_CRITICAL_EVIDENCE` | 路由与意图 | high | 有 vault 搜索但未命中 |
| `IRRELEVANT_CONTEXT` | 路由与意图 | medium | context 来源与问题无关 |
| `UNSUPPORTED_CLAIM` | 路由与意图 | critical | 声称"我记得"但未调读取工具 |
| `APPROVAL_BYPASS` | 路由与意图 | critical | 绕过审批 |
| `CONTEXT_OVERFLOW` | 路由与意图 | medium | 上下文超预算 |
| `WRONG_TOOL` | 工具与执行 | high | 选择的工具与意图不符 |
| `WRONG_TOOL_ARGUMENT` | 工具与执行 | high | 工具参数错误 |
| `FALSE_COMPLETION` | 工具与执行 | critical | 声称完成但无写入 |
| `DUPLICATE_SIDE_EFFECT` | 工具与执行 | high | 重复执行 |
| `TASK_STATE_LOST` | 工具与执行 | high | 任务状态丢失 |
| `TOOL_RECOVERY_FAILED` | 工具与执行 | medium | 工具失败后未恢复 |
| `MEMORY_WRITE_FALSE_POSITIVE` | 记忆与数据 | medium | 情绪/临时状态写入 |
| `MEMORY_RECALL_MISS` | 记忆与数据 | high | 相关记忆未召回 |
| `MEMORY_CONFLICT_NOT_RESOLVED` | 记忆与数据 | medium | 冲突未覆盖 |

数据闭环:
```
Trace → Failure Code → Candidate Case → 修复 → Regression Case
```

Golden case 晋升时可标注 `failure_code` 字段表示该 case 预期暴露的失败类型。
