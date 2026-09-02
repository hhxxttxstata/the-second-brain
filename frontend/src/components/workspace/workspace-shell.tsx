// Workspace Shell — 三栏总装 + 数据加载 + chat 发送流程（交接文档 §22 无流式策略：
// 发送即建 run placeholder → 请求返回后按 trace_id 拉全量轨迹刷新 AgentRun）
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type RawSummary, type RawTool } from "@/lib/workspace/api";
import {
  chatResponseToRun,
  placeholderRun,
  toWorkspaceSnapshot,
  traceToRun,
} from "@/lib/workspace/adapters";
import { demoWorkspaceSnapshot } from "@/lib/workspace/demo-fallback";
import type { AgentRun, ChatMessage, MemoryDelta, WorkspaceSnapshot } from "@/lib/workspace/types";
import { AgentRunInspector } from "./agent-run-inspector";
import { CommandBar } from "./command-bar";
import { ContextSidebar } from "./context-sidebar";
import { ConversationPanel } from "./conversation-panel";
import { WorkspaceDrawer, type DrawerTab } from "./workspace-drawer";

let msgSeq = 0;
const newId = (p: string) => `${p}_${Date.now()}_${msgSeq++}`;

export function WorkspaceShell() {
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [riskByName, setRiskByName] = useState<Map<string, RawTool>>(new Map());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("tasks");
  const conversationRef = useRef<{ role: "user" | "assistant"; content: string }[]>([]);

  const currentRun = useMemo(
    () => runs.find((r) => r.id === currentRunId) ?? null,
    [runs, currentRunId],
  );
  const runsById = useMemo(() => new Map(runs.map((r) => [r.id, r])), [runs]);

  const loadSummary = useCallback(async () => {
    try {
      const raw: RawSummary = await api.workspaceSummary();
      setSnapshot(toWorkspaceSnapshot(raw));
      setSnapshotError(null);
      // 首屏无会话内 run 时，用最近一条真实 trace 填充 Inspector
      if (raw.latest_run) {
        setRuns((prev) =>
          prev.length > 0 ? prev : [traceToRun(raw.latest_run!, riskByName)],
        );
      }
    } catch (e) {
      // Level C：后端不可达 → demo fixture 保底，UI 明确标注
      setSnapshot(demoWorkspaceSnapshot());
      setSnapshotError(e instanceof Error ? e.message : String(e));
    }
  }, [riskByName]);

  useEffect(() => {
    api.tools()
      .then((t) => setRiskByName(new Map(t.tools.map((tool) => [tool.name, tool]))))
      .catch(() => undefined);
    loadSummary();
  }, [loadSummary]);

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
        const resp = await api.chat(
          text,
          conversationRef.current.map((c) => `${c.role === "user" ? "User" : "Assistant"}: ${c.content}`),
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
        const finalRun = chatResponseToRun(resp, trace, riskByName);
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
        conversationRef.current = [
          ...conversationRef.current,
          { role: "user", content: text },
          { role: "assistant", content: resp.result ?? "" },
        ];
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
    [loadSummary, riskByName],
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
        <p className="shrink-0 bg-warn-soft px-4 py-1 text-[11px] text-warn">
          后端不可达（{snapshotError}）— 当前展示 demo 数据。
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
