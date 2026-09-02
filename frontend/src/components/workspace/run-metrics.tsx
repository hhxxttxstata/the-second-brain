// Run Metrics — Inspector 底部：Latency / Agents / Tools / Failures / Tokens
import type { AgentRun } from "@/lib/workspace/types";
import { formatDuration } from "@/lib/workspace/adapters";

export function RunMetrics({ run }: { run: AgentRun }) {
  const m = run.metrics;
  const items: { label: string; value: string | number; tone?: "err" | "ok" }[] = [
    { label: "Latency", value: m.latencyMs != null ? formatDuration(m.latencyMs) : "—" },
    { label: "Agents", value: m.agentCount ?? "—" },
    { label: "Tools", value: m.toolCount ?? 0 },
    { label: "Failures", value: m.failureCount ?? 0, tone: (m.failureCount ?? 0) > 0 ? "err" : "ok" },
    { label: "Tokens", value: m.tokens != null ? m.tokens.toLocaleString() : "—" },
  ];

  return (
    <div className="grid grid-cols-5 gap-px overflow-hidden rounded-md border border-edge-soft bg-edge-soft">
      {items.map((it) => (
        <div key={it.label} className="bg-base-900 px-2 py-2 text-center">
          <p
            className={`truncate font-mono text-sm leading-tight ${
              it.tone === "err" ? "text-err" : it.tone === "ok" ? "text-ok" : "text-ink"
            }`}
          >
            {it.value}
          </p>
          <p className="mt-0.5 text-[9px] uppercase tracking-wider text-faint">{it.label}</p>
        </div>
      ))}
    </div>
  );
}
