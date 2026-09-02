// Action Strip — 每轮 Agent 回答下方的执行摘要（交接文档 §6.3 最重要组件之一）
// 只展示 action / route / status / latency，不展示 CoT。
import { Check, Loader2, X } from "lucide-react";
import type { AgentRun } from "@/lib/workspace/types";
import { formatDuration } from "@/lib/workspace/adapters";
import { Tag } from "./ui";

function StepIcon({ status }: { status: "success" | "failed" | "running" | "pending" }) {
  switch (status) {
    case "success":
      return <Check size={11} className="shrink-0 text-ok" />;
    case "failed":
      return <X size={11} className="shrink-0 text-err" />;
    case "running":
      return <Loader2 size={11} className="shrink-0 animate-spin text-run" />;
    default:
      return <span className="inline-block h-2 w-2 shrink-0 rounded-full border border-faint" />;
  }
}

export function ActionStrip({ run }: { run: AgentRun }) {
  const agentSteps = run.steps.filter((s) => s.kind !== "tool");
  const failures = run.steps.filter((s) => s.status === "failed");

  const headline =
    run.status === "running"
      ? "Executing"
      : failures.length > 0
        ? "Partial completion"
        : "Executed";

  const headlineTone =
    run.status === "running" ? "text-run" : failures.length > 0 ? "text-warn" : "text-mute";

  const summaryBits = [
    run.metrics.toolCount ? `${run.metrics.toolCount} tools` : null,
    run.metrics.latencyMs ? formatDuration(run.metrics.latencyMs) : null,
  ].filter(Boolean);

  return (
    <div className="mt-2 rounded-md border border-edge-soft bg-base-900/70 px-2.5 py-2">
      <div className="mb-1.5 flex items-center justify-between">
        <span className={`text-[10px] font-semibold uppercase tracking-wider ${headlineTone}`}>
          {headline}
        </span>
        {summaryBits.length > 0 && (
          <span className="font-mono text-[10px] text-faint">{summaryBits.join(" · ")}</span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {agentSteps.map((s) => (
          <span key={s.id} className="inline-flex items-center gap-1 text-xs">
            <StepIcon status={s.status} />
            <span className={s.status === "failed" ? "text-err" : s.status === "pending" ? "text-faint" : "text-mute"}>
              {s.label}
            </span>
            {s.errorCode && <Tag tone="err">{s.errorCode}</Tag>}
          </span>
        ))}
      </div>
      {failures.length > 0 && (
        <p className="mt-1.5 border-t border-edge-soft pt-1.5 font-mono text-[10px] text-err">
          Failure: {[...new Set(failures.map((f) => f.errorCode).filter(Boolean))].join(", ") || "run failed"}
        </p>
      )}
      {run.memoryDeltas.length > 0 && (
        <p className="mt-1.5 font-mono text-[10px] text-faint">
          Memory: +{run.memoryDeltas.length} write{run.memoryDeltas.length > 1 ? "s" : ""}
        </p>
      )}
    </div>
  );
}
