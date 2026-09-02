// 中间 Main — Conversation Header / 消息流 / Input Composer
import { useEffect, useRef, useState } from "react";
import { Database, ListTodo, SendHorizontal } from "lucide-react";
import type { AgentRun, ChatMessage, WorkspaceSnapshot } from "@/lib/workspace/types";
import { MessageItem } from "./message-item";
import { EmptyState, Spinner } from "./ui";

export function ConversationPanel({
  snapshot,
  messages,
  runsById,
  sending,
  onSend,
}: {
  snapshot: WorkspaceSnapshot | null;
  messages: ChatMessage[];
  runsById: Map<string, AgentRun>;
  sending: boolean;
  onSend: (text: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const pressure = snapshot?.pressure;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, sending]);

  const submit = () => {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft("");
    onSend(text);
  };

  const pressureLabel =
    pressure == null
      ? "Context: Normal"
      : `Context Pressure: ${(pressure.usageRatio * 100).toFixed(0)}% · ${pressure.level}`;

  return (
    <section className="flex min-w-[420px] flex-1 flex-col overflow-hidden">
      {/* Conversation Header */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-b border-edge bg-base-900/60 px-4 py-2">
        <div className="min-w-0">
          <p className="truncate text-xs font-semibold text-ink">Research Session</p>
          <p className="font-mono text-[10px] text-faint">Personal AI Workspace</p>
        </div>
        <div className="flex-1" />
        <div className="flex items-center gap-3 font-mono text-[10px] text-mute">
          <span className="inline-flex items-center gap-1">
            <Database size={10} className="text-faint" />
            Memory: {snapshot?.recentMemories.length ?? 0}
          </span>
          <span className="inline-flex items-center gap-1">
            <ListTodo size={10} className="text-faint" />
            Tasks: {snapshot?.today.activeTasks ?? 0}
          </span>
          <span
            className={
              pressure?.level === "red"
                ? "text-err"
                : pressure?.level === "yellow"
                  ? "text-warn"
                  : "text-faint"
            }
          >
            {pressureLabel}
          </span>
        </div>
      </div>

      {/* messages */}
      <div className="flex-1 overflow-y-auto py-2">
        {messages.length === 0 ? (
          <EmptyState
            title="No conversation yet"
            hint="发一条消息，观察 Supervisor → 子 Agent → 工具 → Final 的完整执行轨迹。"
          />
        ) : (
          messages.map((m, i) => {
            // 助手消息往前找最近的用户消息，作为反馈 bad case 的 input
            const inputText =
              m.role === "assistant"
                ? [...messages.slice(0, i)].reverse().find((x) => x.role === "user")?.content
                : undefined;
            return (
              <MessageItem
                key={m.id}
                message={m}
                run={m.runId ? runsById.get(m.runId) : undefined}
                inputText={inputText}
              />
            );
          })
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input Composer */}
      <div className="shrink-0 border-t border-edge bg-base-900 p-3">
        <div className="flex items-end gap-2 rounded-lg border border-edge bg-base-850 p-2 focus-within:border-accent/60">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={2}
            placeholder="发消息给 Agent — 例如：总结今天的讨论，记录关键结论，并创建后续任务…"
            className="max-h-40 flex-1 resize-none bg-transparent text-sm text-ink outline-none placeholder:text-faint"
          />
          <button
            onClick={submit}
            disabled={sending || !draft.trim()}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent text-white transition-opacity disabled:opacity-40"
            title="Send"
          >
            {sending ? <Spinner /> : <SendHorizontal size={15} />}
          </button>
        </div>
        <p className="mt-1 px-1 font-mono text-[10px] text-faint">
          Enter 发送 · Shift+Enter 换行 · 无流式，响应需等待 Agent 完整执行
        </p>
      </div>
    </section>
  );
}
