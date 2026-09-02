// Demo fixture — 交接文档 §23 的三个演示案例。
// 仅在后端不可达或真实数据为空时使用，source: "demo" 明确标注，不冒充真实运行结果。
import type { AgentRun, WorkspaceSnapshot } from "./types";

const NOW = () => new Date().toISOString();

/** Case A — 正常多动作：Supervisor → Reflect → Memory → Plan → Chat */
export function demoRunMultiAction(): AgentRun {
  const id = "demo_run_multi_action";
  return {
    id,
    status: "success",
    startedAt: NOW(),
    finishedAt: NOW(),
    route: "multi-task",
    routeReason: "总结 + 记忆写入 + 任务创建，多动作请求",
    steps: [
      { id: `${id}-sup`, kind: "supervisor", label: "Supervisor", status: "success", action: "route → multi-task", detail: "总结今天关于 Agent 评测的讨论，把关键结论记下来，并创建明天继续修改的任务。" },
      { id: `${id}-reflect`, kind: "reflect", label: "Reflect Agent", status: "success", action: "分析当前任务与架构问题", durationMs: 1240 },
      { id: `${id}-memory`, kind: "memory", label: "Memory Agent", status: "success", action: "search_memory + write_memory", durationMs: 430 },
      { id: `${id}-plan`, kind: "plan", label: "Plan Agent", status: "success", action: "update_task → 明天继续 trajectory evaluator", durationMs: 260 },
      { id: `${id}-chat`, kind: "chat", label: "Chatbot", status: "success", action: "compose final summary", durationMs: 2100 },
      { id: `${id}-final`, kind: "final", label: "Final", status: "success", detail: "已完成总结，并更新长期记忆和后续任务。", durationMs: 4030 },
    ],
    tools: [
      { id: `${id}-t1`, name: "search_memory", status: "success", durationMs: 45, riskLevel: "low" },
      { id: `${id}-t2`, name: "write_memory", status: "success", durationMs: 385, riskLevel: "medium" },
      { id: `${id}-t3`, name: "create_handoff", status: "success", durationMs: 120, riskLevel: "medium" },
    ],
    metrics: { latencyMs: 4030, agentCount: 4, toolCount: 3, failureCount: 0, tokens: 3210 },
    memoryDeltas: [
      { id: `${id}-md1`, operation: "create", type: "episodic", content: "Agent 评测需要 trajectory 级指标（workflow completion / required action recall）", sourceRunId: id, timestamp: NOW() },
      { id: `${id}-md2`, operation: "create", type: "task", content: "明天继续改造 trajectory evaluator", sourceRunId: id, timestamp: NOW() },
    ],
    source: "demo",
  };
}

/** Case B — Tool Failure：write_memory → TOOL_TIMEOUT */
export function demoRunToolFailure(): AgentRun {
  const id = "demo_run_tool_failure";
  return {
    id,
    status: "partial",
    startedAt: NOW(),
    finishedAt: NOW(),
    route: "memory",
    routeReason: "请求要求持久化",
    steps: [
      { id: `${id}-sup`, kind: "supervisor", label: "Supervisor", status: "success", action: "route → memory", detail: "把这个决定记下来" },
      { id: `${id}-memory`, kind: "memory", label: "Memory Agent", status: "failed", action: "write_memory", durationMs: 5000, errorCode: "TOOL_TIMEOUT" },
      { id: `${id}-final`, kind: "final", label: "Final", status: "failed", detail: "Partial completion: memory write failed", errorCode: "TOOL_TIMEOUT" },
    ],
    tools: [
      { id: `${id}-t1`, name: "search_memory", status: "success", durationMs: 52, riskLevel: "low" },
      { id: `${id}-t2`, name: "write_memory", status: "failed", durationMs: 5000, riskLevel: "medium", error: "TOOL_TIMEOUT" },
    ],
    metrics: { latencyMs: 5120, agentCount: 1, toolCount: 2, failureCount: 1, tokens: 890 },
    memoryDeltas: [],
    source: "demo",
  };
}

/** Case C — Candidate：expected 4 agents, actual 只有 Chat → PREMATURE_STOP */
export function demoRunCandidate(): AgentRun {
  const id = "demo_run_premature_stop";
  return {
    id,
    status: "partial",
    startedAt: NOW(),
    finishedAt: NOW(),
    route: "chatbot",
    routeReason: "误判为普通对话",
    steps: [
      { id: `${id}-sup`, kind: "supervisor", label: "Supervisor", status: "success", action: "route → chatbot", detail: "预期应拆分为多任务" },
      { id: `${id}-chat`, kind: "chat", label: "Chatbot", status: "success", action: "仅完成对话部分", durationMs: 1500 },
      { id: `${id}-final`, kind: "final", label: "Final", status: "success", detail: "Premature stop: Reflect / Memory / Plan 未执行" },
    ],
    tools: [],
    metrics: { latencyMs: 1500, agentCount: 1, toolCount: 0, failureCount: 1, tokens: 640 },
    memoryDeltas: [],
    source: "demo",
  };
}

export const demoCandidates = [
  { id: "C-103", intent: "premature-stop", failureType: "PREMATURE_STOP", status: "candidate" as const, expected: ["Chat", "Reflect", "Memory", "Plan"], actual: ["Chat"] },
  { id: "C-102", intent: "memory-conflict", failureType: "MEMORY_CONFLICT_NOT_RESOLVED", status: "candidate" as const, expected: ["Memory resolve"], actual: ["Memory write"] },
  { id: "C-101", intent: "tool-argument", failureType: "WRONG_TOOL_ARGUMENT", status: "fixed" as const, expected: ["valid params"], actual: ["schema mismatch"] },
];

/** 整页 fallback snapshot：后端不可达时保住三栏布局有内容可看 */
export function demoWorkspaceSnapshot(): WorkspaceSnapshot {
  return {
    system: { status: "healthy", successRate: 96.4, totalTraces: 417 },
    today: { activeTasks: 2, memoryUpdates: 5, reflections: 2, failedRuns: 1, totalRuns: 9 },
    activeTasks: [
      {
        id: "task_012",
        title: "task_012",
        status: "active",
        goal: "Agent Evaluation v3 — 多动作轨迹评测",
        nextAction: "实现 required_action_recall 判分",
        handoffContent: "## Goal\nBuild multi-action trajectory evaluation\n\n## Next step\nImplement required_action_recall",
        updatedAt: NOW(),
      },
      {
        id: "task_011",
        title: "task_011",
        status: "pending_approval",
        goal: "补齐 supervisor 单路由下的 golden 覆盖",
        pendingTool: "create_handoff",
        nextAction: "等待审批后执行",
        updatedAt: NOW(),
      },
    ],
    recentMemories: [
      { id: "d1", type: "profile", content: "偏好 architecture-first 的回答方式", createdAt: NOW(), source: "conversation" },
      { id: "d2", type: "episodic", content: "Agent eval 需要 trajectory 指标", createdAt: NOW() },
      { id: "d3", type: "task", content: "RAG v3 evaluation refactor — in progress", createdAt: NOW() },
    ],
    evaluation: {
      golden: { passed: 24, total: 24 },
      safety: { passed: 6, total: 6 },
      multiTurn: { passed: 4, total: 4 },
      traceability: 81,
      totalScore: 78.5,
      workflowCompletionRate: 92,
      latestRegression: { status: "pass", at: NOW(), promotionGate: "PASS" },
      candidates: demoCandidates.map((c) => ({
        id: c.id, intent: c.intent, failureType: c.failureType, status: c.status,
        knownIssue: `Expected: ${c.expected.join(" + ")} / Actual: ${c.actual.join(" + ")}`,
      })),
      failureStats: [
        { code: "TOOL_TIMEOUT", count: 3 },
        { code: "MEMORY_CONFLICT_NOT_RESOLVED", count: 2 },
        { code: "PREMATURE_END", count: 1 },
      ],
      source: "demo",
    },
    memoryDeltasToday: [
      { id: "demo-delta-1", operation: "create", type: "episodic", content: "User prefers architecture-first explanation", sourceRunId: "demo_run_multi_action", timestamp: NOW() },
      { id: "demo-delta-2", operation: "create", type: "task", content: "明天继续改造 trajectory evaluator", sourceRunId: "demo_run_multi_action", timestamp: NOW() },
    ],
    pressure: { usageRatio: 0.42, level: "green" },
    source: "demo",
  };
}
