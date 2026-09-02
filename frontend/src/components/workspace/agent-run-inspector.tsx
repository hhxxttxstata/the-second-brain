// 右侧 Inspector — Current Run：Agent Timeline + Tool Calls + Run Metrics
import { Radio, Wrench } from "lucide-react";
import type { AgentRun } from "@/lib/workspace/types";
import { AgentTimeline } from "./agent-timeline";
import { RunMetrics } from "./run-metrics";
import { ToolCallItem } from "./tool-call-item";
import { EmptyState, SectionTitle, StatusBadge } from "./ui";

export function AgentRunInspector({
  run,
  live,
}: {
  run: AgentRun | null;
  live: boolean;
}) {
  return (
    <aside className="flex w-[340px] shrink-0 flex-col overflow-hidden border-l border-edge bg-base-900">
      <div className="shrink-0 border-b border-edge px-3 py-2.5">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-1.5 text-sm font-semibold text-ink">
            <Radio size={13} className={live ? "text-run" : "text-faint"} />
            Current Run
          </h2>
          {run && (
            <StatusBadge
              tone={
                run.status === "running"
                  ? "run"
                  : run.status === "success"
                    ? "ok"
                    : run.status === "partial"
                      ? "warn"
                      : run.status === "failed"
                        ? "err"
                        : "mute"
              }
              pulse={run.status === "running"}
            >
              {run.status}
            </StatusBadge>
          )}
        </div>
        <p className="mt-0.5 truncate font-mono text-[10px] text-faint">
          {run ? `#${run.id}` : "no run"}
        </p>
        {run?.routeReason && (
          <p className="mt-1 line-clamp-2 text-[11px] text-mute">
            <span className="font-mono text-faint">Reason: </span>
            {run.routeReason}
          </p>
        )}
      </div>

      {!run ? (
        <div className="flex flex-1 items-center justify-center">
          <EmptyState
            title="No active run"
            hint="Agent execution trace will appear here."
          />
        </div>
      ) : (
        <div className="flex-1 space-y-4 overflow-y-auto p-3">
          <section>
            <SectionTitle>Agent Timeline</SectionTitle>
            <AgentTimeline run={run} />
          </section>

          <section>
            <SectionTitle
              right={
                <span className="inline-flex items-center gap-1 font-mono text-[10px] text-faint">
                  <Wrench size={10} />
                  {run.tools.length}
                </span>
              }
            >
              Tool Calls
            </SectionTitle>
            {run.tools.length === 0 ? (
              <p className="px-1 py-2 text-[11px] text-faint">本轮未调用工具。</p>
            ) : (
              <div className="mt-1 space-y-1">
                {run.tools.map((t) => (
                  <ToolCallItem key={t.id} tool={t} />
                ))}
              </div>
            )}
          </section>

          <section>
            <SectionTitle>Run Metrics</SectionTitle>
            <div className="mt-1">
              <RunMetrics run={run} />
            </div>
          </section>

          {run.memoryDeltas.length > 0 && (
            <section>
              <SectionTitle>Memory Delta</SectionTitle>
              <div className="mt-1 space-y-1">
                {run.memoryDeltas.map((d) => (
                  <div key={d.id} className="rounded border border-edge-soft bg-base-850 px-2 py-1.5">
                    <span className="font-mono text-[10px] text-ok">+ created</span>
                    <span className="ml-1.5 font-mono text-[10px] text-faint">{d.type}</span>
                    <p className="mt-0.5 line-clamp-2 text-[11px] text-mute">{d.content}</p>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </aside>
  );
}
