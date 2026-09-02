// Tasks Tab — Task Handoff 卡片（不是普通 todo）：Goal / Next Action / Pending Tool / 状态
import type { TaskHandoff } from "@/lib/workspace/types";
import { timeAgo } from "@/lib/workspace/adapters";
import { EmptyState, StatusBadge, Tag } from "./ui";

export function TaskPanel({ tasks }: { tasks: TaskHandoff[] }) {
  if (tasks.length === 0) {
    return <EmptyState title="No active task" hint="Start a conversation or continue a previous handoff." />;
  }

  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-3">
      {tasks.map((t) => (
        <div key={t.id} className="flex flex-col rounded-md border border-edge-soft bg-base-850 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-xs text-accent">{t.id}</span>
            <StatusBadge tone={t.status === "pending_approval" ? "warn" : "accent"}>
              {t.status === "pending_approval" ? "待审批" : t.status}
            </StatusBadge>
          </div>

          {t.goal && (
            <div className="mt-2">
              <p className="text-[10px] uppercase tracking-wider text-faint">Goal</p>
              <p className="mt-0.5 line-clamp-2 text-xs text-ink">{t.goal}</p>
            </div>
          )}

          {t.nextAction && (
            <div className="mt-2">
              <p className="text-[10px] uppercase tracking-wider text-faint">Next Action</p>
              <p className="mt-0.5 line-clamp-2 text-xs text-mute">{t.nextAction}</p>
            </div>
          )}

          {t.pendingTool && (
            <div className="mt-2">
              <Tag tone="warn">pending_tool: {t.pendingTool}</Tag>
            </div>
          )}

          <div className="flex-1" />
          <p className="mt-2 border-t border-edge-soft pt-1.5 font-mono text-[10px] text-faint">
            Handoff · {t.updatedAt ? timeAgo(t.updatedAt) : "active"}
          </p>
        </div>
      ))}
    </div>
  );
}
