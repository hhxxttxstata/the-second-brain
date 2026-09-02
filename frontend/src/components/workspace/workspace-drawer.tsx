// Bottom Drawer — 第二层信息：Tasks | Memory | Evaluation | Failures
// 默认收起为 40px Tab Bar，展开约 42% 高度（交接文档 §8）
import type { ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { EvaluationSnapshot, MemoryDelta, MemoryRecord, TaskHandoff } from "@/lib/workspace/types";
import { EvaluationPanel } from "./evaluation-panel";
import { FailurePanel } from "./failure-panel";
import { MemoryPanel } from "./memory-panel";
import { TaskPanel } from "./task-panel";

export type DrawerTab = "tasks" | "memory" | "evaluation" | "failures";

const TABS: { key: DrawerTab; label: string }[] = [
  { key: "tasks", label: "Tasks" },
  { key: "memory", label: "Memory" },
  { key: "evaluation", label: "Evaluation" },
  { key: "failures", label: "Failures" },
];

export function WorkspaceDrawer({
  tab,
  open,
  tasks,
  memories,
  deltas,
  evaluation,
  onTabChange,
  onToggle,
}: {
  tab: DrawerTab;
  open: boolean;
  tasks: TaskHandoff[];
  memories: MemoryRecord[];
  deltas: MemoryDelta[];
  evaluation: EvaluationSnapshot | null;
  onTabChange: (tab: DrawerTab) => void;
  onToggle: () => void;
}) {
  let body: ReactNode = null;
  if (open) {
    switch (tab) {
      case "tasks":
        body = <TaskPanel tasks={tasks} />;
        break;
      case "memory":
        body = <MemoryPanel memories={memories} deltas={deltas} />;
        break;
      case "evaluation":
        body = <EvaluationPanel ev={evaluation} />;
        break;
      case "failures":
        body = <FailurePanel ev={evaluation} />;
        break;
    }
  }

  return (
    <div
      className={`flex shrink-0 flex-col border-t border-edge bg-base-900 transition-[height] duration-200 ${
        open ? "h-[42vh]" : "h-10"
      }`}
    >
      {/* Tab Bar */}
      <div className="flex h-10 shrink-0 items-center gap-1 px-3">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => {
              if (t.key === tab) {
                onToggle();
              } else {
                onTabChange(t.key);
                if (!open) onToggle();
              }
            }}
            className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
              t.key === tab && open
                ? "bg-base-800 text-ink"
                : "text-mute hover:bg-base-850 hover:text-ink"
            }`}
          >
            {t.label}
            {t.key === "tasks" && tasks.length > 0 && (
              <span className="ml-1.5 font-mono text-[10px] text-faint">{tasks.length}</span>
            )}
            {t.key === "failures" && (evaluation?.failureStats.length ?? 0) > 0 && (
              <span className="ml-1.5 rounded bg-err-soft px-1 font-mono text-[10px] text-err">
                {evaluation!.failureStats.reduce((a, b) => a + b.count, 0)}
              </span>
            )}
          </button>
        ))}
        <div className="flex-1" />
        <button
          onClick={onToggle}
          className="rounded p-1 text-mute hover:bg-base-800 hover:text-ink"
          title={open ? "Collapse" : "Expand"}
        >
          {open ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
        </button>
      </div>

      {/* Body */}
      {open && <div className="min-h-0 flex-1 overflow-y-auto border-t border-edge-soft p-3">{body}</div>}
    </div>
  );
}
