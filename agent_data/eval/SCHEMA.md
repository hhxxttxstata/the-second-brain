# 测试用例字段标准

## 总览

| 层级 | 必填 | 选填 | 文件 |
|---|---|---|---|
| candidate | input, tags | intent, note | `candidate/*.json` |
| exploratory | input, expected_route, tags, fixture_needed, ablation_trigger | intent, note | `exploratory/*.json` |
| challenge | 全部必填字段 | note | `challenge/*.json` |
| golden | 全部必填字段 | regression_badge, badcase_from, note | `golden/*.json` |

---

## 字段清单

```jsonc
{
  // ── 1. 身份 (所有层级必填) ──
  "id": "BC5-diary-routing",
  // 唯一标识。惯例: {badcase或特征}-{简短描述}，全小写 kebab-case
  // candidate 层可以不加，晋升时补

  "intent": "日记检索",
  // 一句话描述测什么。不超过 20 字
  // 用于命令行输出和 grader 报告

  "input": "我最近写了什么日记？",
  // 用户实际输入文本。与生产环境完全一致

  "tags": ["routing", "vault_search", "memory_retrieval"],
  // 能力维度标签，用于筛选和组合评测维度
  // 参考标签表（见下文）

  // ── 2. 行为定义 (golden/challenge 必填) ──

  "expected_route": "chatbot",
  // 期望的路由目标: chatbot | plan | reflect | memory
  // 注意: 这个字段不描述其它行为，只描述路由

  "required_outcomes": [
    "路由到 chatbot",
    "回答基于 vault 日记内容（非编造）",
    "至少引用一篇具体日记"
  ],
  // 必须全部满足的行为。Grader 逐条检查，有一条不通过 → 失败
  // 每个元素是具体、可判定的条件（不是主观描述）
  // ✅ "输出包含至少一条 todo"  ← 可判定
  // ❌ "回答质量高"             ← 不可判定

  "forbidden_actions": [
    "路由到 memory",
    "返回编造的日记内容",
    "回答 '我没有找到相关信息' 但 vault 中有匹配内容"
  ],
  // 零容忍行为。发生任意一条 → 失败
  // 每个元素也是具体、可判定的条件

  // ── 3. 环境定义 (golden/challenge 必填) ──

  "fixture_needed": {
    "profile": "用户塔塔，26岁，医学影像算法方向",
    "vault": "需有至少3篇近期日记（含7月22日1篇）",
    "task_memory": "需有1条待办 '2027秋招offer'",
    "episodic_memory": "需有情绪相关条目",
    "today_state": "需有今日状态信息",
    "time": "需模拟特定日期（如 2026-07-26）"
  },
  // 初始环境的不可变预设条件。
  // Key=条件维度, Value=具体要求
  // 所有 key 都是可选的——只写需要的
  // 标准 key 表: profile, vault, task_memory, episodic_memory, today_state, time, permissions

  "fixture_setup": [
    "task_memory 中新增一条 priority=high 的 '2027秋招offer' todo",
    "profile.json 写入 name=塔塔 age=26",
    "确保 vault/diary/ 下有 2026-07-22 的日记文件"
  ],
  // 可执行的 setup 指令。Runner 可在跑前自动执行
  // (可选，fixture_needed 的文字版即可)

  // ── 4. 边界分析 (golden/challenge 强推荐) ──

  "edge_cases": [
    "vault 中无匹配内容 → 应返回'未找到'而非编造",
    "任务已存在且状态为 done → 不应重复创建"
  ],
  // 相邻的边界条件。不在 required_outcomes 中体现但 Grader 应覆盖
  // 用于防止修复 A 时破坏 B

  "reasonable_alternatives": [
    "用中文回复也接受，不强制英文",
    "把 plan 项显示为编号列表或要点列表均可"
  ],
  // 哪些行为虽然与 reference 不同，但仍应通过
  // 防止 Grader 过于死板

  // ── 5. 元数据 ──

  "stage": "candidate | exploratory | challenge | golden",
  // 当前所处生命周期阶段

  "substage": null,
  // golden 的细分: "regression" | "dataset" | null
  // 不设默认值——由文件位置决定

  "regression_badge": true,
  // true → 该 golden case 是 regression（每次提交必过）
  // 仅 golden 可用

  "badcase_from": "BC5 - 路由错误",
  // 来自哪个 badcase。regression_badge=true 时必填

  "ablation_trigger": "vault 内容变化时回答质量退化",
  // 什么条件下该用例可能退化。仅 exploratory 使用

  "known_issue": "当前路由到 chatbot 而非 memory",
  // 当前已知失败原因。仅 challenge/candidate 使用。
  // golden 层如果还有 known_issue → 降级回 challenge

  "failure_code": "MEMORY_RECALL_MISS",
  // 该 case 预期暴露的失败码（用于 Failure Taxonomy）
  // golden/challenge 晋升时建议标注此字段
  // 合法值见 app/agent/failure_taxonomy.py 的 ALL_FAILURE_CODES

  "note": "补充说明",
  // 任何额外的上下文

  "created_at": "2026-07-26",
  // 用例创建日期。晋升时不修改

  "reference_solution": "agent eval 连续通过 3 次",
  // reference solution 的通过纪录。晋升时更新
}
```

## 标签表

`tags` 用于组合筛选评测维度。每个用例至少 1 个，建议 2-3 个。

| 标签 | 说明 | 适用对象 |
|---|---|---|
| `routing` | 测路由是否正确 | supervisor / orchestrator |
| `memory_read` | 记忆读取 | 记忆系统 |
| `memory_write` | 记忆写入（含去重/合并） | 记忆系统 |
| `vault_search` | 知识库搜索 | vault / RAG |
| `planning` | 计划生成 | plan agent |
| `reflection` | 反思分析 | reflect agent |
| `self_intro` | 自我介绍能力 | chatbot |
| `contradiction` | 矛盾指令处理 | orchestrator |
| `multi_turn` | 多轮上下文 tracking | conversation |
| `external_api` | 依赖外部 API | 工具层 |
| `ambiguous` | 模糊/发散输入 | 意图提取 |
| `fixture_dependent` | 依赖特定 fixture | 环境准备 |

## 阶段晋升时的字段补齐

```
candidate → exploratory:
  + expected_route, fixture_needed (至少 1 个 key)
  + ablation_trigger 或 note
  + tags

exploratory → challenge:
  + required_outcomes (至少 1 条)
  + forbidden_actions (至少 1 条)
  + 补全 fixture_needed
  + known_issue（如果当前失败）

challenge → golden:
  + 补全 required_outcomes (≥2 条)
  + 补全 forbidden_actions (≥1 条)
  - 去掉 known_issue（不再有已知失败）
  + 可能添加 edge_cases、reasonable_alternatives

golden → regression:
  + regression_badge: true
  + badcase_from
  + reference_solution
```

## 反例 — 不加的字段

| 字段 | 不加的原因 |
|---|---|
| `expected_output` | 精确匹配输出太死板，合理替代方案会被误杀 |
| `latency_lt` | agent 在一台机器上延迟不代表另一台，不纳入定义 |
| `expected_answer_content` | 内容因人而异，用 required_outcomes 描述行为 |
| `difficulty` | 主观、不稳定，用 stage 替代 |
| `author` | 不需要追溯作者 |
