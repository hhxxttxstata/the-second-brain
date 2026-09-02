// Workspace 视图模型 — 前端统一数据契约。
// 组件只依赖这些类型；后端原始字段在 adapters.ts 统一转换。

export type SystemStatus = "healthy" | "warning" | "error";
export type RunStatus = "idle" | "running" | "success" | "partial" | "failed";
export type StepStatus = "pending" | "running" | "success" | "failed";
export type AgentKind =
  | "supervisor"
  | "chat"
  | "plan"
  | "reflect"
  | "memory"
  | "tool"
  | "final";
export type MemoryLayer = "profile" | "episodic" | "task";
export type MemoryOperation = "create" | "update" | "invalidate";
export type DataSource = "live" | "demo";

export interface ToolCallView {
  id: string;
  name: string;
  status: "success" | "failed" | "running";
  durationMs?: number;
  riskLevel?: string;
  sideEffect?: "read" | "write";
  error?: string;
  input?: unknown;
  outputPreview?: string;
}

export interface AgentStep {
  id: string;
  kind: AgentKind;
  label: string;
  status: StepStatus;
  action?: string;
  durationMs?: number;
  detail?: string;
  input?: unknown;
  output?: unknown;
  errorCode?: string;
}

export interface AgentRun {
  id: string;
  status: RunStatus;
  startedAt?: string;
  finishedAt?: string;
  route?: string;
  routeReason?: string;
  steps: AgentStep[];
  tools: ToolCallView[];
  metrics: {
    latencyMs?: number;
    agentCount?: number;
    toolCount?: number;
    failureCount?: number;
    tokens?: number;
  };
  memoryDeltas: MemoryDelta[];
  source: DataSource;
}

export interface MemoryRecord {
  id: string;
  type: MemoryLayer;
  content: string;
  createdAt?: string;
  source?: string;
  importance?: number;
}

export interface MemoryDelta {
  id: string;
  operation: MemoryOperation;
  type: MemoryLayer;
  before?: string;
  after?: string;
  content?: string;
  sourceRunId?: string;
  timestamp?: string;
}

export interface TaskHandoff {
  id: string;
  title: string;
  status: "active" | "pending_approval" | "completed";
  goal?: string;
  pendingTool?: string;
  nextAction?: string;
  handoffContent?: string;
  updatedAt?: string;
}

export interface EvaluationCandidate {
  id: string;
  intent: string;
  failureType: string;
  status: "candidate" | "fixed" | "regression" | "promoted";
  expectedRoute?: string;
  knownIssue?: string;
  createdAt?: string;
  traceRef?: string;
}

export interface FailureStat {
  code: string;
  count: number;
}

export interface ScoreItem {
  passed: number;
  total: number;
}

export interface EvaluationSnapshot {
  golden: ScoreItem | null;
  safety: ScoreItem | null;
  multiTurn: ScoreItem | null;
  traceability: number | null;
  totalScore: number | null;
  workflowCompletionRate: number | null;
  latestRegression: {
    status: "pass" | "fail";
    at?: string;
    promotionGate?: string;
    p95DeltaPct?: number;
  } | null;
  candidates: EvaluationCandidate[];
  failureStats: FailureStat[];
  source: DataSource;
}

export interface WorkspaceSnapshot {
  system: {
    status: SystemStatus;
    successRate?: number;
    totalTraces?: number;
  };
  today: {
    activeTasks: number;
    memoryUpdates: number;
    reflections: number;
    failedRuns: number;
    totalRuns: number;
  };
  activeTasks: TaskHandoff[];
  recentMemories: MemoryRecord[];
  evaluation: EvaluationSnapshot | null;
  /** 今天所有 run 产生的记忆变化（来自 trace.memory_updates） */
  memoryDeltasToday: MemoryDelta[];
  pressure: { usageRatio: number; level: "green" | "yellow" | "red" } | null;
  source: DataSource;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  /** 该轮回复对应的 run（assistant 消息才有） */
  runId?: string;
}

export type RunEventListener = (run: AgentRun) => void;
