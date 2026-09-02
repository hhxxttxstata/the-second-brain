// 顶部 Command Bar（56px）— 品牌 / Active Task / 系统健康 / 评测快照
import { Activity, FlaskConical, Settings } from "lucide-react";
import type { WorkspaceSnapshot } from "@/lib/workspace/types";
import { StatusBadge } from "./ui";

export function CommandBar({
  snapshot,
  onOpenDrawer,
}: {
  snapshot: WorkspaceSnapshot | null;
  onOpenDrawer: (tab: "tasks" | "memory" | "evaluation" | "failures") => void;
}) {
  const status = snapshot?.system.status ?? "warning";
  const golden = snapshot?.evaluation?.golden;
  const activeTask = snapshot?.activeTasks?.[0];

  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-edge bg-base-900 px-4">
      {/* brand */}
      <div className="flex items-baseline gap-2">
        <span className="text-sm font-semibold tracking-tight text-ink">Second Brain</span>
        <span className="font-mono text-[11px] text-faint">/ Workspace</span>
      </div>

      {/* active task */}
      <button
        onClick={() => onOpenDrawer("tasks")}
        className="hidden min-w-0 max-w-[360px] items-center gap-2 rounded border border-edge-soft bg-base-850 px-2.5 py-1 text-left transition-colors hover:border-base-750 md:flex"
      >
        <span className="shrink-0 text-[10px] uppercase tracking-wider text-faint">Active Task</span>
        <span className="truncate text-xs text-ink">
          {activeTask ? activeTask.goal || activeTask.id : "No active task"}
        </span>
      </button>

      <div className="flex-1" />

      {/* system status + eval pulse */}
      <button onClick={() => onOpenDrawer("evaluation")} className="flex items-center gap-2">
        <StatusBadge tone={status === "healthy" ? "ok" : status === "warning" ? "warn" : "err"} pulse={status !== "healthy"}>
          <Activity size={11} className="mr-0.5" />
          Agent {status === "healthy" ? "Healthy" : status === "warning" ? "Degraded" : "Error"}
        </StatusBadge>
        {golden && (
          <StatusBadge tone={golden.passed === golden.total && golden.total > 0 ? "ok" : "warn"}>
            <FlaskConical size={11} className="mr-0.5" />
            Golden {golden.passed}/{golden.total}
          </StatusBadge>
        )}
      </button>

      {snapshot?.source === "demo" && (
        <span className="rounded border border-warn/40 bg-warn-soft px-1.5 py-0.5 font-mono text-[10px] text-warn">
          DEMO DATA
        </span>
      )}

      <a
        href="/docs"
        target="_blank"
        rel="noreferrer"
        title="API Docs"
        className="rounded p-1.5 text-mute transition-colors hover:bg-base-800 hover:text-ink"
      >
        <Settings size={15} />
      </a>
    </header>
  );
}
