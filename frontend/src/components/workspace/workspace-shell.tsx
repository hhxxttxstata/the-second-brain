// Workspace Shell — 三栏总装 + 数据加载 + chat 发送流程（交接文档 §22 无流式策略：
// 发送即建 run placeholder → 请求返回后按 trace_id 拉全量轨迹刷新 AgentRun）
// 会话持久化：活跃会话的 messages/runs 是权威源，每次变化写穿 localStorage
// （session-store），刷新后由 bootstrap() 惰性恢复；历史会话仅显式归档才从
// 主列表移除，归档可恢复。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type RawSummary, type RawTool } from "@/lib/workspace/api";
import {
  chatResponseToRun,
  placeholderRun,
  toWorkspaceSnapshot,
  traceToRun,
} from "@/lib/workspace/adapters";
import { demoWorkspaceSnapshot } from "@/lib/workspace/demo-fallback";
import {
  conversationFromMessages,
  createSession,
  loadActiveSessionId,
  loadSessions,
  persistSessions,
  reviveSession,
  sessionTitleFrom,
  upsertSession,
  type StoredSession,
} from "@/lib/workspace/session-store";
import type { AgentRun, ChatMessage, MemoryDelta, WorkspaceSnapshot } from "@/lib/workspace/types";
import { AgentRunInspector } from "./agent-run-inspector";
import { CommandBar } from "./command-bar";
import { ContextSidebar } from "./context-sidebar";
import { ConversationPanel } from "./conversation-panel";
import { WorkspaceDrawer, type DrawerTab } from "./workspace-drawer";

let msgSeq = 0;
const newId = (p: string) => `${p}_${Date.now()}_${msgSeq++}`;

/** 用实时 messages/runs 覆盖会话记录（活跃会话的内存态是权威源） */
function withLive(
  record: StoredSession,
  messages: ChatMessage[],
  runs: AgentRun[],
): StoredSession {
  return {
    ...record,
    messages,
    runs,
    title: messages.length > 0 ? sessionTitleFrom(messages) : record.title,
  };
}

/** 按最后活动时间取最近的活跃（未归档）会话 */
function latestActive(sessions: StoredSession[]): StoredSession | null {
  const active = sessions.filter((s) => !s.archived);
  if (active.length === 0) return null;
  return [...active].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0];
}

/** mount 时的会话恢复：上次活跃会话（已修复刷新打断的 run）或新建空会话。
 *  只读 localStorage + 幂等回写，无副作用积累，StrictMode 双跑收敛。 */
function bootstrap(): {
  sessions: StoredSession[];
  activeId: string;
  messages: ChatMessage[];
  runs: AgentRun[];
} {
  // 空会话不落盘后，localStorage 里 0 消息的记录只会是历史遗留存根，直接清掉
  const list = loadSessions().filter((s) => s.messages.length > 0);
  const target =
    list.find((s) => s.id === loadActiveSessionId() && !s.archived) ?? latestActive(list);
  if (target) {
    const revived = reviveSession(target);
    const nextList = upsertSession(list, revived);
    persistSessions(nextList, revived.id);
    return { sessions: nextList, activeId: revived.id, messages: revived.messages, runs: revived.runs };
  }
  const fresh = createSession();
  // 空会话不落盘：只记录 activeId，首次发消息后由写穿 effect 持久化
  persistSessions(list, fresh.id);
  return { sessions: [fresh, ...list], activeId: fresh.id, messages: [], runs: [] };
}

export function WorkspaceShell() {
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [boot] = useState(bootstrap);
  const [sessions, setSessions] = useState<StoredSession[]>(boot.sessions);
  const [activeId, setActiveId] = useState<string | null>(boot.activeId);
  const [messages, setMessages] = useState<ChatMessage[]>(boot.messages);
  const [runs, setRuns] = useState<AgentRun[]>(boot.runs);
  const [currentRunId, setCurrentRunId] = useState<string | null>(boot.runs[0]?.id ?? null);
  const [sending, setSending] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("tasks");
  // 工具 risk 表用 ref 而非 state：loadSummary/sendMessage 依赖它但不依赖其渲染结果，
  // 放 state 里会让 useCallback 链、mount effect 和请求形成无限循环
  const riskRef = useRef<Map<string, RawTool>>(new Map());

  const currentRun = useMemo(
    () => runs.find((r) => r.id === currentRunId) ?? null,
    [runs, currentRunId],
  );
  const runsById = useMemo(() => new Map(runs.map((r) => [r.id, r])), [runs]);

  // ---- 会话管理 ----
  // sessions state 里活跃会话的记录可能落后于实时 messages/runs；
  // 所有读活跃记录的地方一律先 mergedSessions() 并回实时内容。

  const mergedSessions = useCallback(() => {
    const current = activeId ? sessions.find((s) => s.id === activeId) : null;
    if (!current) return sessions;
    return upsertSession(sessions, {
      ...withLive(current, messages, runs),
      updatedAt: new Date().toISOString(),
    });
  }, [sessions, activeId, messages, runs]);

  // 活跃会话内容变化 → 写穿 localStorage（外部系统同步，不改 React state）。
  // 空会话不落盘：避免 loadSummary 的 latest_run 兜底 run 混进会话记录。
  useEffect(() => {
    if (!activeId || messages.length === 0) return;
    const current = sessions.find((s) => s.id === activeId);
    if (!current) return;
    persistSessions(
      upsertSession(sessions, {
        ...withLive(current, messages, runs),
        updatedAt: new Date().toISOString(),
      }),
      activeId,
    );
  }, [messages, runs, activeId, sessions]);

  const switchSession = useCallback(
    (id: string) => {
      if (id === activeId) return;
      const list = mergedSessions();
      const target = list.find((s) => s.id === id);
      if (!target) return;
      // 归档条目点击 = 恢复并直接打开
      const nextList = target.archived
        ? upsertSession(list, { ...target, archived: false })
        : list;
      // 必须回写 sessions state：merged 结果此前只进了 localStorage，
      // 不回写会导致切走再切回时拿到过期消息
      setSessions(nextList);
      persistSessions(nextList, id);
      setActiveId(id);
      setMessages(target.messages);
      setRuns(target.runs);
      setCurrentRunId(target.runs[0]?.id ?? null);
    },
    [mergedSessions, activeId],
  );

  const newSession = useCallback(() => {
    const fresh = createSession();
    const list = mergedSessions();
    // 空会话不落盘：首次发消息后由写穿 effect 持久化
    persistSessions(list, fresh.id);
    setSessions([fresh, ...list]);
    setActiveId(fresh.id);
    setMessages([]);
    setRuns([]);
    setCurrentRunId(null);
  }, [mergedSessions]);

  const archiveSession = useCallback(
    (id: string) => {
      let list = mergedSessions();
      const target = list.find((s) => s.id === id);
      if (!target || target.archived) return;
      list = upsertSession(list, { ...target, archived: true });
      if (id === activeId) {
        // 归档的是当前会话：切到最近的其他活跃会话，没有则新建
        const next = latestActive(list);
        if (next) {
          persistSessions(list, next.id);
          setActiveId(next.id);
          setMessages(next.messages);
          setRuns(next.runs);
          setCurrentRunId(next.runs[0]?.id ?? null);
          setSessions(list);
        } else {
          const fresh = createSession();
          // 空会话不落盘：首次发消息后由写穿 effect 持久化
          persistSessions(list, fresh.id);
          setSessions([fresh, ...list]);
          setActiveId(fresh.id);
          setMessages([]);
          setRuns([]);
          setCurrentRunId(null);
        }
      } else {
        persistSessions(list, activeId);
        setSessions(list);
      }
    },
    [mergedSessions, activeId],
  );

  const unarchiveSession = useCallback(
    (id: string) => {
      const target = sessions.find((s) => s.id === id);
      if (!target || !target.archived) return;
      const nextList = upsertSession(sessions, { ...target, archived: false });
      persistSessions(nextList, activeId);
      setSessions(nextList);
    },
    [sessions, activeId],
  );

  // 仅允许删除归档会话；活跃会话走归档路径（防误删）
  const deleteSession = useCallback(
    (id: string) => {
      const target = sessions.find((s) => s.id === id);
      if (!target || !target.archived) return;
      const nextList = sessions.filter((s) => s.id !== id);
      persistSessions(nextList, activeId);
      setSessions(nextList);
    },
    [sessions, activeId],
  );

  // 展示用列表：活跃会话永远用实时内容覆盖
  const displaySessions = useMemo(
    () =>
      sessions.map((s) => {
        if (s.id !== activeId) return s;
        const current = withLive(s, messages, runs);
        return current;
      }),
    [sessions, activeId, messages, runs],
  );

  // ---- 数据加载 ----

  const loadSummary = useCallback(async () => {
    try {
      const raw: RawSummary = await api.workspaceSummary();
      setSnapshot(toWorkspaceSnapshot(raw));
      setSnapshotError(null);
      // 首屏无会话内 run 时，用最近一条真实 trace 填充 Inspector
      if (raw.latest_run) {
        setRuns((prev) =>
          prev.length > 0 ? prev : [traceToRun(raw.latest_run!, riskRef.current)],
        );
      }
    } catch (e) {
      // Level C：后端不可达 → demo fixture 保底，UI 明确标注
      setSnapshot(demoWorkspaceSnapshot());
      setSnapshotError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    api.tools()
      .then((t) => {
        riskRef.current = new Map(t.tools.map((tool) => [tool.name, tool]));
      })
      .catch(() => undefined);
    loadSummary();
    // 反馈提交成功后刷新快照（Evaluation 抽屉立即显示新回流 candidate）
    const onFeedback = () => loadSummary();
    window.addEventListener("ws:feedback-submitted", onFeedback);
    return () => window.removeEventListener("ws:feedback-submitted", onFeedback);
  }, [loadSummary]);

  // 后端暂时不可达（重启/网络抖动）时每 15s 重试，恢复后横幅自动消失。
  // 用 interval 而非 timeout：连续失败时错误文案不变，state 不触发 effect 重跑。
  useEffect(() => {
    if (!snapshotError) return;
    const timer = setInterval(() => loadSummary(), 15_000);
    return () => clearInterval(timer);
  }, [snapshotError, loadSummary]);

  const openDrawer = useCallback((tab: DrawerTab) => {
    setDrawerTab(tab);
    setDrawerOpen(true);
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
      const userMsg: ChatMessage = {
        id: newId("msg"),
        role: "user",
        content: text,
        createdAt: new Date().toISOString(),
      };
      const run = placeholderRun();
      const assistantMsg: ChatMessage = {
        id: newId("msg"),
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
        runId: run.id,
      };
      setMessages((m) => [...m, userMsg, assistantMsg]);
      setRuns((r) => [run, ...r]);
      setCurrentRunId(run.id);
      setSending(true);

      try {
        // event_id = 会话窗口唯一ID：后端按它累积 checkpoint 历史；
        // conversation 回传仅在空线程（首次/老会话迁移）时作为种子被采用
        const resp = await api.chat(
          text,
          conversationFromMessages(messages).map(
            (c) => `${c.role === "user" ? "User" : "Assistant"}: ${c.content}`,
          ),
          activeId ?? undefined,
        );

        // 完成后按 trace_id 补拉完整执行轨迹（tool calls / memory deltas / failure codes）
        let trace = null;
        if (resp.trace_id) {
          try {
            trace = await api.runTrace(resp.trace_id);
          } catch {
            /* trace 拉取失败不阻塞主流程 */
          }
        }
        const finalRun = chatResponseToRun(resp, trace, riskRef.current);
        setRuns((r) => r.map((x) => (x.id === run.id ? finalRun : x)));
        setCurrentRunId(finalRun.id);
        setMessages((m) =>
          m.map((x) =>
            x.id === assistantMsg.id
              ? {
                  ...x,
                  content: resp.result || resp.error || "（Agent 未返回内容）",
                  runId: finalRun.id,
                }
              : x,
          ),
        );
      } catch (e) {
        const failed: AgentRun = {
          ...run,
          status: "failed",
          steps: run.steps.map((s) =>
            s.status === "running" || s.status === "pending"
              ? { ...s, status: "failed" as const, errorCode: "REQUEST_FAILED" }
              : s,
          ),
        };
        setRuns((r) => r.map((x) => (x.id === run.id ? failed : x)));
        setMessages((m) =>
          m.map((x) =>
            x.id === assistantMsg.id
              ? { ...x, content: `请求失败：${e instanceof Error ? e.message : String(e)}` }
              : x,
          ),
        );
      } finally {
        setSending(false);
        // 非阻塞刷新工作台快照（今日计数 / 记忆 / 评测）
        loadSummary();
      }
    },
    [loadSummary, messages, activeId],
  );

  // Inspector 展示当前 run；无会话内 run 时回退最近一条历史 trace
  const viewRun: AgentRun | null = currentRun ?? runs[0] ?? null;
  const memoryDeltas: MemoryDelta[] =
    currentRun && currentRun.memoryDeltas.length > 0
      ? currentRun.memoryDeltas
      : (snapshot?.memoryDeltasToday ?? []);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-base-950">
      <CommandBar snapshot={snapshot} onOpenDrawer={openDrawer} />

      {snapshotError && (
        <p className="flex shrink-0 items-center gap-2 bg-warn-soft px-4 py-1 text-[11px] text-warn">
          <span>
            后端不可达（{snapshotError}）— 当前展示 demo 数据，恢复后自动切换。
          </span>
          <button
            onClick={() => loadSummary()}
            className="rounded border border-warn/40 px-1.5 py-0.5 text-[10px] text-warn hover:bg-warn/10"
          >
            立即重试
          </button>
        </p>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="hidden lg:flex">
          <ContextSidebar snapshot={snapshot} onOpenDrawer={openDrawer} />
        </div>
        <ConversationPanel
          snapshot={snapshot}
          messages={messages}
          runsById={runsById}
          sending={sending}
          onSend={sendMessage}
          sessions={displaySessions}
          activeSessionId={activeId}
          onSwitchSession={switchSession}
          onNewSession={newSession}
          onArchiveSession={archiveSession}
          onUnarchiveSession={unarchiveSession}
          onDeleteSession={deleteSession}
        />
        <div className="hidden lg:flex">
          <AgentRunInspector run={viewRun} live={viewRun?.status === "running"} />
        </div>
      </div>

      <WorkspaceDrawer
        tab={drawerTab}
        open={drawerOpen}
        tasks={snapshot?.activeTasks ?? []}
        memories={snapshot?.recentMemories ?? []}
        deltas={memoryDeltas}
        evaluation={snapshot?.evaluation ?? null}
        onTabChange={setDrawerTab}
        onToggle={() => setDrawerOpen((v) => !v)}
      />
    </div>
  );
}
