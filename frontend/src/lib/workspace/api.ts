// 后端 API 封装 — 开发期走 Vite proxy（同源），生产可经 VITE_API_BASE 指向 FastAPI。

const BASE: string = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function fetchJson<T>(path: string, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, { ...init, signal: controller.signal });
    if (!res.ok) {
      throw new ApiError(`${init?.method ?? "GET"} ${path} → ${res.status}`, res.status);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

/** 后端原始返回类型（只在 adapter 里解包，组件不接触） */

export interface RawChatResponse {
  success: boolean;
  route?: string;
  route_reason?: string;
  result?: string;
  result_data?: Record<string, unknown>;
  tasks?: { agent: string; instruction: string; stage?: number }[];
  task_results?: {
    index?: number;
    agent: string;
    instruction?: string;
    success: boolean;
    result?: string;
    error?: string | null;
  }[];
  latency_ms?: number;
  run_id?: string;
  trace_id?: string;
  error?: string;
}

export interface RawTrace {
  trace_id: string;
  task_type?: string;
  user_intent?: string;
  timestamp?: string;
  tool_calls?: {
    name: string;
    params?: Record<string, unknown>;
    result_preview?: string;
    latency_ms?: number;
    success?: boolean;
    error?: string | null;
  }[];
  llm_model?: string;
  total_tokens?: number;
  final_output?: string;
  success?: boolean;
  error?: string | null;
  latency_ms?: number;
  memory_updates?: { type?: string; preview?: string; timestamp?: string }[];
  step_log?: { step: string; detail: string; timestamp: string }[];
  failure_codes?: string[] | null;
}

export interface RawSummary {
  system: {
    status: string;
    trace_stats?: Record<string, unknown> & { workspace_success_rate?: number; total?: number };
  };
  today: {
    active_tasks: number;
    memory_updates: number;
    reflections: number;
    failed_runs: number;
    total_runs: number;
  };
  active_tasks: {
    task_id: string;
    status: string;
    goal?: string;
    approval_key?: string;
    pending_tool?: string;
    handoff_content?: string;
  }[];
  recent_memories: {
    id: number | string;
    memory_type: string;
    content: string;
    importance?: number;
    source?: string;
    created_at?: string;
  }[];
  latest_run: RawTrace | null;
  evaluation: {
    benchmark: {
      timestamp?: string;
      total_cases?: number;
      pass_rate?: number;
      workflow_completion_rate?: number;
      regression_guard?: {
        prev_pass_rate?: number;
        cur_pass_rate?: number;
        p95_latency_delta_pct?: number;
        promotion_gate?: string;
      } | null;
    } | null;
    multi_turn: { timestamp?: string; total_tasks?: number; pass_rate?: number } | null;
    scorecard: {
      timestamp?: string;
      total_score?: number | string;
      level_scores?: Record<string, string | number>;
    } | null;
    candidates: {
      id: string;
      intent?: string;
      stage?: string;
      expected_route?: string;
      known_issue?: string;
      trace_ref?: string;
      created_at?: string;
      failure_codes?: string[];
    }[];
    failure_distribution: {
      by_code?: Record<string, number>;
      failure_rate?: number;
      total_traces?: number;
    } | null;
    memory_deltas_today: {
      operation: string;
      type: string;
      content: string;
      source_run_id?: string;
      timestamp?: string;
    }[];
  };
  pressure: {
    usage_ratio: number;
    level: string;
  } | null;
}

export interface RawTool {
  name: string;
  description?: string;
  risk_level?: string;
}

export const api = {
  workspaceSummary: (timeoutMs = 30_000) =>
    fetchJson<RawSummary>("/workspace/summary", undefined, timeoutMs),

  runTrace: (traceId: string) => fetchJson<RawTrace>(`/workspace/runs/${traceId}`),

  chat: (
    text: string,
    conversation: string[],
    timeoutMs = 180_000,
  ): Promise<RawChatResponse> =>
    fetchJson<RawChatResponse>(
      "/agent/v2/chat",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          user_id: "default_user",
          // 后端 ChatRequest.conversation 是 list[str]；orchestrator 取最近 5 条注入 planner
          conversation: conversation.slice(-10),
        }),
      },
      timeoutMs,
    ),

  traces: (limit = 30) =>
    fetchJson<{ traces: RawTrace[]; stats: Record<string, unknown> }>(
      `/agent/v2/traces?limit=${limit}`,
    ),

  tools: () =>
    fetchJson<{ tool_count: number; tools: RawTool[] }>("/agent/v2/tools"),
};
