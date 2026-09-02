// 左侧 Sidebar — Personal Operating Context：Today / Active Task / Recent Memory / Evolution
import { Brain, ChevronRight } from "lucide-react";
import type { WorkspaceSnapshot } from "@/lib/workspace/types";
import { timeAgo } from "@/lib/workspace/adapters";
import { Card, EmptyState, SectionTitle, StatusBadge } from "./ui";

export function ContextSidebar({
  snapshot,
  onOpenDrawer,
}: {
  snapshot: WorkspaceSnapshot | null;
  onOpenDrawer: (tab: "tasks" | "memory" | "evaluation" | "failures") => void;
}) {
  const today = snapshot?.today;
  const activeTask = snapshot?.activeTasks?.[0];
  const memories = snapshot?.recentMemories?.slice(0, 3) ?? [];
  const ev = snapshot?.evaluation;

  return (
    <aside className="flex w-[260px] shrink-0 flex-col gap-4 overflow-y-auto border-r border-edge bg-base-900 p-3">
      {/* Today */}
      <section className="space-y-1.5">
        <SectionTitle>Today</SectionTitle>
        <Card className="grid grid-cols-2 gap-px overflow-hidden border-edge-soft bg-edge-soft">
          <Metric label="Active Tasks" value={today?.activeTasks} tone={today?.activeTasks ? "ink" : "mute"} />
          <Metric label="Memory Updates" value={today?.memoryUpdates} />
          <Metric label="Reflections" value={today?.reflections} />
          <Metric label="Failed Runs" value={today?.failedRuns} tone={today?.failedRuns ? "err" : "mute"} />
        </Card>
      </section>

      {/* Active Task */}
      <section className="space-y-1.5">
        <SectionTitle
          right={
            <button onClick={() => onOpenDrawer("tasks")} className="text-faint hover:text-ink" title="Open Tasks">
              <ChevronRight size={12} />
            </button>
          }
        >
          Active Task
        </SectionTitle>
        {activeTask ? (
          <Card className="space-y-2 p-2.5" onClick={() => onOpenDrawer("tasks")}>
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-xs text-accent">{activeTask.id}</span>
              <StatusBadge tone={activeTask.status === "pending_approval" ? "warn" : "accent"}>
                {activeTask.status === "pending_approval" ? "待审批" : "in_progress"}
              </StatusBadge>
            </div>
            {activeTask.goal && <p className="line-clamp-2 text-xs text-ink">{activeTask.goal}</p>}
            {activeTask.nextAction && (
              <div className="border-t border-edge-soft pt-1.5">
                <p className="text-[10px] uppercase tracking-wider text-faint">Next Action</p>
                <p className="line-clamp-2 text-xs text-mute">{activeTask.nextAction}</p>
              </div>
            )}
          </Card>
        ) : (
          <Card className="p-1">
            <EmptyState title="No active task" hint="开始对话或继续历史 handoff。" />
          </Card>
        )}
      </section>

      {/* Recent Memory */}
      <section className="space-y-1.5">
        <SectionTitle
          right={
            <button onClick={() => onOpenDrawer("memory")} className="text-faint hover:text-ink" title="Open Memory">
              <ChevronRight size={12} />
            </button>
          }
        >
          Recent Memory
        </SectionTitle>
        {memories.length > 0 ? (
          <div className="space-y-1">
            {memories.map((m) => (
              <div key={m.id} className="rounded border border-edge-soft bg-base-850 px-2 py-1.5">
                <div className="flex items-start gap-1.5">
                  <span className={`font-mono text-xs ${m.type === "profile" ? "text-accent" : m.type === "episodic" ? "text-ok" : "text-warn"}`}>
                    +
                  </span>
                  <p className="line-clamp-2 flex-1 text-xs text-ink">{m.content}</p>
                </div>
                <p className="mt-0.5 pl-4 font-mono text-[10px] text-faint">
                  {m.type} · {timeAgo(m.createdAt) || "—"}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <Card className="p-1">
            <EmptyState title="No recent memory" hint="记忆写入后会出现在这里。" />
          </Card>
        )}
      </section>

      {/* Evolution Status */}
      <section className="space-y-1.5">
        <SectionTitle
          right={
            <button onClick={() => onOpenDrawer("evaluation")} className="text-faint hover:text-ink" title="Open Evaluation">
              <ChevronRight size={12} />
            </button>
          }
        >
          Self Evolution
        </SectionTitle>
        <Card className="space-y-1.5 p-2.5" onClick={() => onOpenDrawer("evaluation")}>
          <EvalRow label="Golden" value={ev?.golden} />
          <EvalRow label="Multi-turn" value={ev?.multiTurn} />
          <div className="flex items-center justify-between text-xs">
            <span className="text-mute">Traceability</span>
            <span className="font-mono text-ink">
              {ev?.traceability != null ? `${ev.traceability}%` : "—"}
            </span>
          </div>
          {ev?.candidates?.length ? (
            <div className="flex items-center gap-1.5 border-t border-edge-soft pt-1.5">
              <Brain size={11} className="shrink-0 text-run" />
              <span className="truncate font-mono text-[10px] text-mute">
                Latest candidate · {ev.candidates[0].id}
              </span>
            </div>
          ) : null}
        </Card>
      </section>

      <div className="flex-1" />

      <p className="px-1 text-[10px] leading-relaxed text-faint">
        Personal AI Workspace
        <br />
        Plan · Remember · Reflect · Act · Improve
      </p>
    </aside>
  );
}

function Metric({
  label,
  value,
  tone = "ink",
}: {
  label: string;
  value?: number;
  tone?: "ink" | "mute" | "err";
}) {
  return (
    <div className="bg-base-900 px-2.5 py-2">
      <p
        className={`font-mono text-base leading-none ${
          tone === "err" ? "text-err" : tone === "mute" ? "text-mute" : "text-ink"
        }`}
      >
        {value ?? 0}
      </p>
      <p className="mt-1 text-[10px] text-faint">{label}</p>
    </div>
  );
}

function EvalRow({ label, value }: { label: string; value?: { passed: number; total: number } | null }) {
  const done = value && value.total > 0 && value.passed === value.total;
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-mute">{label}</span>
      <span className={`font-mono ${done ? "text-ok" : "text-ink"}`}>
        {value ? `${value.passed} / ${value.total}` : "—"}
      </span>
    </div>
  );
}
