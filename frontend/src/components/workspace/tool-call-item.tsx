// Tool Call 条目 — 工具名 / 状态 / risk_level / 耗时 / 错误码
import type { ToolCallView } from "@/lib/workspace/types";
import { formatDuration } from "@/lib/workspace/adapters";
import { Tag } from "./ui";

export function ToolCallItem({ tool }: { tool: ToolCallView }) {
  const failed = tool.status === "failed";
  const riskTone = tool.riskLevel === "high" ? "err" : tool.riskLevel === "medium" ? "warn" : "mute";

  return (
    <div className="flex items-center gap-2 rounded border border-edge-soft bg-base-850 px-2 py-1.5">
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${failed ? "bg-err" : "bg-ok"}`}
        title={failed ? "failed" : "success"}
      />
      <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink">{tool.name}</span>
      {tool.riskLevel && <Tag tone={riskTone}>risk: {tool.riskLevel}</Tag>}
      {tool.durationMs != null && (
        <span className="font-mono text-[10px] text-faint">{formatDuration(tool.durationMs)}</span>
      )}
      <span className={`shrink-0 font-mono text-[10px] ${failed ? "text-err" : "text-ok"}`}>
        {failed ? "FAILED" : "success"}
      </span>
    </div>
  );
}
