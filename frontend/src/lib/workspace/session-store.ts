// 会话本地存储 — 历史会话持久化到 localStorage，刷新不丢失；
// 仅当用户显式归档后才从主列表移除（归档可恢复）。
// 后端 /agent/v2/chat 不含 session 概念，会话管理完全在前端本地完成。
import type { AgentRun, ChatMessage } from "./types";

export interface StoredSession {
  id: string;
  /** 首条用户消息截断，空会话为"新会话" */
  title: string;
  createdAt: string;
  updatedAt: string;
  archived: boolean;
  messages: ChatMessage[];
  runs: AgentRun[];
}

const SESSIONS_KEY = "ws.sessions.v1";
const ACTIVE_KEY = "ws.activeSession.v1";

// 配额保护：localStorage 约 5MB，超限时从最旧的归档/会话尾部裁剪
const MAX_MESSAGES_PER_SESSION = 200;
const MAX_RUNS_PER_SESSION = 30;
const MAX_ACTIVE_SESSIONS = 40;
const MAX_ARCHIVED_SESSIONS = 30;
// run step/tool 里的 input/output 可能是整段 trace，超长文本持久化前截断
const MAX_PERSIST_TEXT = 4000;

let seq = 0;

export function createSession(): StoredSession {
  const now = new Date().toISOString();
  return {
    id: `sess_${Date.now()}_${seq++}`,
    title: "新会话",
    createdAt: now,
    updatedAt: now,
    archived: false,
    messages: [],
    runs: [],
  };
}

export function sessionTitleFrom(messages: ChatMessage[]): string {
  const first = messages.find((m) => m.role === "user")?.content ?? "";
  const line = first.split("\n")[0].trim();
  return line ? (line.length > 40 ? `${line.slice(0, 40)}…` : line) : "新会话";
}

// ---------------------------------------------------------------- persist

/** 持久化前裁剪：截断超长文本、限制 messages/runs 条数 */
function slimSession(session: StoredSession): StoredSession {
  const slimText = (v: unknown): unknown => {
    if (typeof v === "string") return v.length > MAX_PERSIST_TEXT ? `${v.slice(0, MAX_PERSIST_TEXT)}…` : v;
    if (Array.isArray(v)) return v.map(slimText);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, slimText(x)]));
    }
    return v;
  };
  const slimRun = (r: AgentRun): AgentRun => ({
    ...r,
    steps: r.steps.map((s) => ({
      ...s,
      detail: typeof s.detail === "string" ? s.detail.slice(0, MAX_PERSIST_TEXT) : s.detail,
      action: typeof s.action === "string" ? s.action.slice(0, MAX_PERSIST_TEXT) : s.action,
      input: s.input === undefined ? undefined : (slimText(s.input) as AgentRun["steps"][0]["input"]),
      output: s.output === undefined ? undefined : (slimText(s.output) as AgentRun["steps"][0]["output"]),
    })),
    tools: r.tools.map((t) => ({
      ...t,
      input: t.input === undefined ? undefined : (slimText(t.input) as typeof t.input),
      outputPreview:
        t.outputPreview && t.outputPreview.length > MAX_PERSIST_TEXT
          ? `${t.outputPreview.slice(0, MAX_PERSIST_TEXT)}…`
          : t.outputPreview,
    })),
  });
  return {
    ...session,
    messages: session.messages.slice(-MAX_MESSAGES_PER_SESSION),
    runs: session.runs.slice(0, MAX_RUNS_PER_SESSION).map(slimRun),
  };
}

function trimSessions(sessions: StoredSession[]): StoredSession[] {
  const active = sessions.filter((s) => !s.archived).slice(0, MAX_ACTIVE_SESSIONS);
  const archived = sessions.filter((s) => s.archived).slice(0, MAX_ARCHIVED_SESSIONS);
  return [...active, ...archived];
}

function writeSessions(sessions: StoredSession[]): boolean {
  // 写入失败（配额超限）时逐级裁剪：先丢最旧归档，再丢最旧活跃会话
  let list = trimSessions(sessions).map(slimSession);
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      localStorage.setItem(SESSIONS_KEY, JSON.stringify(list));
      return true;
    } catch {
      if (list.some((s) => s.archived)) {
        const oldestArchived = [...list].filter((s) => s.archived).sort((a, b) => a.updatedAt.localeCompare(b.updatedAt))[0];
        list = list.filter((s) => s.id !== oldestArchived.id);
      } else if (list.length > 1) {
        const oldest = [...list].sort((a, b) => a.updatedAt.localeCompare(b.updatedAt))[0];
        list = list.filter((s) => s.id !== oldest.id);
      } else {
        return false;
      }
    }
  }
  return false;
}

export function loadSessions(): StoredSession[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (s): s is StoredSession =>
        !!s && typeof s === "object" && typeof (s as StoredSession).id === "string" && Array.isArray((s as StoredSession).messages),
    );
  } catch {
    return [];
  }
}

export function loadActiveSessionId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

/** 全量写回（新建/切换/归档/恢复后调用）；activeId 一并落盘 */
export function persistSessions(sessions: StoredSession[], activeId: string | null): void {
  writeSessions(sessions);
  if (activeId) {
    try {
      localStorage.setItem(ACTIVE_KEY, activeId);
    } catch {
      /* 配额极端情况下 activeId 可丢弃，不影响会话本体 */
    }
  }
}

export function upsertSession(sessions: StoredSession[], next: StoredSession): StoredSession[] {
  const idx = sessions.findIndex((s) => s.id === next.id);
  if (idx === -1) return [next, ...sessions];
  const copy = [...sessions];
  copy[idx] = next;
  return copy;
}

// ---------------------------------------------------------------- revive

/**
 * 从 localStorage 恢复会话时的修复：刷新打断的 running run 标记为失败，
 * 对应 assistant 空消息补上中断提示，避免出现永远转圈的幽灵轮次。
 */
export function reviveSession(session: StoredSession): StoredSession {
  const interrupted = new Set(
    session.runs.filter((r) => r.status === "running").map((r) => r.id),
  );
  const runs = session.runs.map((r) =>
    interrupted.has(r.id)
      ? {
          ...r,
          status: "failed" as const,
          steps: r.steps.map((s) =>
            s.status === "running" || s.status === "pending"
              ? { ...s, status: "failed" as const, errorCode: "SESSION_INTERRUPTED" }
              : s,
          ),
        }
      : r,
  );
  const messages = session.messages.map((m) =>
    m.role === "assistant" && m.content === "" && m.runId && interrupted.has(m.runId)
      ? { ...m, content: "（刷新页面时该轮请求被中断）" }
      : m,
  );
  return { ...session, messages, runs };
}

/** 由持久化的消息重建多轮上下文（注入 /agent/v2/chat 的 conversation 字段） */
export function conversationFromMessages(messages: ChatMessage[]): { role: "user" | "assistant"; content: string }[] {
  return messages
    .filter((m) => m.content.trim() !== "" && !(m.role === "assistant" && m.content.startsWith("（刷新")))
    .map((m) => ({ role: m.role, content: m.content }));
}
