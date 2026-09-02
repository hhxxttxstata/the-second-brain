// 后端原始 payload → 视图模型。组件永远不直接接触后端字段。
import type {
  AgentRun,
  AgentStep,
  AgentKind,
  EvaluationCandidate,
  EvaluationSnapshot,
  FailureStat,
  MemoryDelta,
  MemoryLayer,
  MemoryRecord,
  SystemStatus,
  TaskHandoff,
  ToolCallView,
  WorkspaceSnapshot,
} from "./types";
import type { RawChatResponse, RawSummary, RawTool, RawTrace } from "./api";

export function agentKindOf(agentName: string): AgentKind {
  const n = agentName.toLowerCase();
  if (n.includes("chatbot") || n.includes("chat")) return "chat";
  if (n.includes("plan")) return "plan";
  if (n.includes("reflect")) return "reflect";
  if (n.includes("memory")) return "memory";
  if (n.includes("supervisor")) return "supervisor";
  return "tool";
}

export function memoryLayerOf(memoryType: string): MemoryLayer {
  if (memoryType === "stable_profile") return "profile";
  if (memoryType === "episodic" || memoryType === "conversation") return "episodic";
  return "task"; // task / task_todos / task_plan
}

function systemStatusOf(raw: RawSummary): SystemStatus {
  const rate = raw.system?.trace_stats?.workspace_success_rate;
  if (typeof rate === "number" && rate < 60) return "error";
  if (typeof rate === "number" && rate < 85) return "warning";
  if (raw.system?.status === "ok" || raw.system?.status === "healthy") return "healthy";
  return "warning";
}

function timeAgo(iso?: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  if (diff < 0) return "just now";
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function formatDuration(ms?: number): string {
  if (ms == null) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

// ---------------------------------------------------------------- tasks

export function toTaskHandoffs(raw: RawSummary["active_tasks"]): TaskHandoff[] {
  return raw.map((t) => ({
    id: t.task_id,
    title: t.task_id,
    status: t.status === "pending_approval" ? "pending_approval" : "active",
    goal: t.goal || undefined,
    pendingTool: t.pending_tool || undefined,
    nextAction: extractNextStep(t.handoff_content),
    handoffContent: t.handoff_content || undefined,
    updatedAt: undefined,
  }));
}

function extractNextStep(handoffContent?: string): string | undefined {
  if (!handoffContent) return undefined;
  const m = handoffContent.match(/##\s*Next step\s*\n+([\s\S]{0,200})/i);
  return m?.[1]?.trim() || undefined;
}

// ---------------------------------------------------------------- memories

export function toMemoryRecords(raw: RawSummary["recent_memories"]): MemoryRecord[] {
  return raw.map((m) => ({
    id: String(m.id),
    type: memoryLayerOf(m.memory_type),
    content: m.content,
    createdAt: m.created_at,
    source: m.source || undefined,
    importance: m.importance,
  }));
}

export function traceMemoryDeltas(trace: RawTrace | null | undefined): MemoryDelta[] {
  if (!trace?.memory_updates?.length) return [];
  return trace.memory_updates.map((mu, i) => ({
    id: `${trace.trace_id}-mem-${i}`,
    operation: "create" as const,
    type: memoryLayerOf(mu.type ?? ""),
    content: mu.preview ?? "",
    sourceRunId: trace.trace_id,
    timestamp: mu.timestamp ?? trace.timestamp,
  }));
}

// ---------------------------------------------------------------- run

let toolSeq = 0;

function toolCallViews(trace: RawTrace, riskByName?: Map<string, RawTool>): ToolCallView[] {
  return (trace.tool_calls ?? []).map((tc) => ({
    id: `${trace.trace_id}-tool-${toolSeq++}`,
    name: tc.name,
    status: tc.success === false ? "failed" : "success",
    durationMs: tc.latency_ms,
    riskLevel: riskByName?.get(tc.name)?.risk_level,
    error: tc.error ?? undefined,
    input: tc.params,
    outputPreview: tc.result_preview,
  }));
}

/** 任意一条 trace → AgentRun（用于 latest_run / 历史 run 展示） */
export function traceToRun(
  trace: RawTrace,
  riskByName?: Map<string, RawTool>,
  source: "live" | "demo" = "live",
): AgentRun {
  const kind = agentKindOf(trace.task_type ?? "");
  const steps: AgentStep[] = [
    {
      id: `${trace.trace_id}-sup`,
      kind: "supervisor",
      label: "Supervisor",
      status: "success",
      action: `route → ${trace.task_type ?? "unknown"}`,
      detail: trace.user_intent,
    },
  ];
  if (kind !== "supervisor") {
    steps.push({
      id: `${trace.trace_id}-agent`,
      kind,
      label: agentLabel(kind),
      status: trace.success === false ? "failed" : "success",
      action: (trace.tool_calls?.length ?? 0) > 0 ? `${trace.tool_calls!.length} tool calls` : "run",
      durationMs: trace.latency_ms,
      errorCode: trace.error ?? undefined,
      output: trace.final_output,
    });
  }
  const tools = toolCallViews(trace, riskByName);
  for (const tc of tools) {
    steps.push({
      id: tc.id,
      kind: "tool",
      label: tc.name,
      status: tc.status === "failed" ? "failed" : "success",
      durationMs: tc.durationMs,
      errorCode: tc.error,
    });
  }
  steps.push({
    id: `${trace.trace_id}-final`,
    kind: "final",
    label: "Final",
    status: trace.success === false ? "failed" : "success",
    detail: trace.final_output?.slice(0, 300),
    durationMs: trace.latency_ms,
  });

  const failureCodes = trace.failure_codes ?? [];
  return {
    id: trace.trace_id,
    status:
      trace.success === false
        ? "failed"
        : failureCodes.length > 0
          ? "partial"
          : "success",
    startedAt: trace.timestamp,
    finishedAt: trace.timestamp,
    route: trace.task_type,
    routeReason: trace.user_intent?.slice(0, 120),
    steps,
    tools,
    metrics: {
      latencyMs: trace.latency_ms,
      agentCount: 1,
      toolCount: tools.length,
      failureCount: failureCodes.length,
      tokens: trace.total_tokens,
    },
    memoryDeltas: traceMemoryDeltas(trace),
    source,
  };
}

export function agentLabel(kind: AgentKind): string {
  switch (kind) {
    case "chat": return "Chatbot";
    case "plan": return "Plan Agent";
    case "reflect": return "Reflect Agent";
    case "memory": return "Memory Agent";
    case "supervisor": return "Supervisor";
    case "tool": return "Tool";
    case "final": return "Final";
  }
}

/** chat 同步响应（+ 完成后补拉的 trace）→ AgentRun */
export function chatResponseToRun(
  resp: RawChatResponse,
  trace: RawTrace | null,
  riskByName?: Map<string, RawTool>,
): AgentRun {
  const runId = resp.trace_id ?? resp.run_id ?? `run_${Date.now()}`;
  const steps: AgentStep[] = [
    {
      id: `${runId}-sup`,
      kind: "supervisor",
      label: "Supervisor",
      status: "success",
      action: `route → ${resp.route ?? "chatbot"}`,
      detail: resp.route_reason,
    },
  ];
  for (const tr of resp.task_results ?? []) {
    const kind = agentKindOf(tr.agent);
    steps.push({
      id: `${runId}-${tr.index ?? steps.length}-${tr.agent}`,
      kind,
      label: agentLabel(kind),
      status: tr.success ? "success" : "failed",
      action: tr.instruction,
      durationMs: trace ? undefined : undefined,
      errorCode: tr.error ?? undefined,
      output: tr.result,
    });
  }
  const tools: ToolCallView[] = trace ? toolCallViews(trace, riskByName) : [];
  for (const tc of tools) {
    steps.push({
      id: tc.id,
      kind: "tool",
      label: tc.name,
      status: tc.status === "failed" ? "failed" : "success",
      durationMs: tc.durationMs,
      errorCode: tc.error,
    });
  }
  const overallSuccess = resp.success && (resp.task_results ?? []).every((t) => t.success);
  steps.push({
    id: `${runId}-final`,
    kind: "final",
    label: "Final",
    status: overallSuccess ? "success" : "failed",
    detail: resp.result?.slice(0, 300),
  });

  const failureCodes = trace?.failure_codes ?? [];
  return {
    id: runId,
    status: !resp.success ? "failed" : overallSuccess && failureCodes.length === 0 ? "success" : "partial",
    startedAt: trace?.timestamp,
    finishedAt: trace?.timestamp,
    route: resp.route,
    routeReason: resp.route_reason,
    steps,
    tools,
    metrics: {
      latencyMs: resp.latency_ms ?? trace?.latency_ms,
      agentCount: (resp.task_results ?? []).length,
      toolCount: tools.length,
      failureCount: failureCodes.length,
      tokens: trace?.total_tokens,
    },
    memoryDeltas: trace ? traceMemoryDeltas(trace) : [],
    source: "live",
  };
}

/** 发送消息瞬间的占位 run（Inspector 立即出现 running 态） */
export function placeholderRun(routeHint = "supervisor"): AgentRun {
  const id = `run_pending_${Date.now()}`;
  return {
    id,
    status: "running",
    route: routeHint,
    steps: [
      { id: `${id}-sup`, kind: "supervisor", label: "Supervisor", status: "running", action: "route request" },
      { id: `${id}-agents`, kind: "chat", label: "Agents", status: "pending", action: "executing sub-agents" },
      { id: `${id}-final`, kind: "final", label: "Final", status: "pending", action: "compose response" },
    ],
    tools: [],
    metrics: {},
    memoryDeltas: [],
    source: "live",
  };
}

// ---------------------------------------------------------------- evaluation

export function toEvaluationSnapshot(raw: RawSummary["evaluation"]): EvaluationSnapshot {
  const bench = raw.benchmark;
  const sc = raw.scorecard;
  const levelScores = sc?.level_scores ?? {};
  const traceabilityRaw = Object.entries(levelScores).find(([k]) => k.startsWith("L5"));
  const traceability = traceabilityRaw ? Number(traceabilityRaw[1]) : null;

  const candidates: EvaluationCandidate[] = (raw.candidates ?? []).map((c) => ({
    id: c.id,
    intent: c.intent ?? "",
    failureType: (c.failure_codes ?? []).join(", ") || "UNKNOWN",
    status: "candidate",
    expectedRoute: c.expected_route,
    knownIssue: c.known_issue,
    createdAt: c.created_at,
    traceRef: c.trace_ref,
  }));

  const failureStats: FailureStat[] = Object.entries(raw.failure_distribution?.by_code ?? {})
    .map(([code, count]) => ({ code, count: Number(count) }))
    .sort((a, b) => b.count - a.count);

  const gate = bench?.regression_guard?.promotion_gate;
  return {
    golden: bench ? { passed: pctToCount(bench.pass_rate, bench.total_cases), total: bench.total_cases ?? 0 } : null,
    safety: null,
    multiTurn: raw.multi_turn
      ? { passed: pctToCount(raw.multi_turn.pass_rate, raw.multi_turn.total_tasks), total: raw.multi_turn.total_tasks ?? 0 }
      : null,
    traceability: Number.isFinite(traceability) ? traceability : null,
    totalScore: sc?.total_score != null ? Number(sc.total_score) : null,
    workflowCompletionRate: bench?.workflow_completion_rate ?? null,
    latestRegression: bench
      ? {
          status: gate === "FAIL" ? "fail" : "pass",
          at: bench.timestamp,
          promotionGate: gate,
          p95DeltaPct: bench.regression_guard?.p95_latency_delta_pct,
        }
      : null,
    candidates,
    failureStats,
    source: "live",
  };
}

function pctToCount(rate?: number, total?: number): number {
  if (rate == null || total == null) return 0;
  return Math.round((rate / 100) * total);
}

// ---------------------------------------------------------------- snapshot

export function toWorkspaceSnapshot(raw: RawSummary): WorkspaceSnapshot {
  const tasks = toTaskHandoffs(raw.active_tasks ?? []);
  const evalSnap = raw.evaluation ? toEvaluationSnapshot(raw.evaluation) : null;
  return {
    system: {
      status: systemStatusOf(raw),
      successRate: raw.system?.trace_stats?.workspace_success_rate,
      totalTraces: raw.system?.trace_stats?.total,
    },
    today: {
      activeTasks: raw.today?.active_tasks ?? 0,
      memoryUpdates: raw.today?.memory_updates ?? 0,
      reflections: raw.today?.reflections ?? 0,
      failedRuns: raw.today?.failed_runs ?? 0,
      totalRuns: raw.today?.total_runs ?? 0,
    },
    activeTasks: tasks,
    recentMemories: toMemoryRecords(raw.recent_memories ?? []),
    evaluation: evalSnap,
    memoryDeltasToday: (raw.evaluation?.memory_deltas_today ?? []).map((d, i) => ({
      id: `delta-today-${i}`,
      operation: (d.operation as MemoryDelta["operation"]) ?? "create",
      type: memoryLayerOf(d.type ?? ""),
      content: d.content,
      sourceRunId: d.source_run_id,
      timestamp: d.timestamp,
    })),
    pressure: raw.pressure
      ? {
          usageRatio: raw.pressure.usage_ratio,
          level: (raw.pressure.level as "green" | "yellow" | "red") ?? "green",
        }
      : null,
    source: "live",
  };
}

export { timeAgo };
