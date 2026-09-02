// Failures Tab — Failure Taxonomy 视觉化：按失败码计数，点击展开说明
import { useMemo, useState } from "react";
import type { EvaluationSnapshot } from "@/lib/workspace/types";
import { EmptyState, Tag } from "./ui";

const FAILURE_EXPLAIN: Record<string, string> = {
  TOOL_TIMEOUT: "工具执行超时 — 检查工具依赖与超时预算",
  MEMORY_CONFLICT_NOT_RESOLVED: "写入的记忆与既有记忆冲突且未消解",
  PREMATURE_END: "过早结束 — 预期的子 Agent / 动作未全部执行",
  ROUTING_ERROR: "Supervisor 路由错误 — 分派了错误的子 Agent",
  WRONG_TOOL: "选择了错误的工具",
  WRONG_TOOL_ARGUMENT: "工具参数不合法",
  CONTEXT_OVERFLOW: "上下文超限触发压缩",
  FALSE_COMPLETION: "声称完成但系统状态未达成",
  WORKFLOW_INCOMPLETE: "多步工作流未走完",
  MISSED_SECONDARY_INTENT: "遗漏了次要意图",
  SIDE_EFFECT_MISSING: "声明了副作用但未实际执行",
  MEMORY_WRITE_FALSE_POSITIVE: "记忆写入返回成功但未生效",
  MEMORY_RECALL_MISS: "应召回的记忆未被检索到",
};

export function FailurePanel({ ev }: { ev: EvaluationSnapshot | null }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const stats = useMemo(() => ev?.failureStats ?? [], [ev]);
  const max = Math.max(1, ...stats.map((s) => s.count));

  if (stats.length === 0) {
    return (
      <EmptyState
        title="No failures recorded"
        hint="当前 traces 中未检出 failure codes — 系统健康。"
      />
    );
  }

  return (
    <div className="space-y-1.5">
      {stats.map((s) => {
        const open = expanded === s.code;
        return (
          <div key={s.code} className="rounded-md border border-edge-soft bg-base-850">
            <button
              onClick={() => setExpanded(open ? null : s.code)}
              className="flex w-full items-center gap-3 px-3 py-2 text-left"
            >
              <span className="w-64 shrink-0 font-mono text-xs text-ink">{s.code}</span>
              <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-base-800">
                <span
                  className="block h-full rounded-full bg-warn/70"
                  style={{ width: `${(s.count / max) * 100}%` }}
                />
              </span>
              <span className="w-8 shrink-0 text-right font-mono text-xs text-warn">{s.count}</span>
            </button>
            {open && (
              <div className="border-t border-edge-soft px-3 py-2">
                <p className="text-[11px] text-mute">
                  {FAILURE_EXPLAIN[s.code] ?? "失败码说明见 failure_taxonomy.py"}
                </p>
                <p className="mt-1 font-mono text-[10px] text-faint">
                  处理路径：bad case → candidate（Evaluation tab）→ fix → regression
                </p>
              </div>
            )}
          </div>
        );
      })}
      <div className="flex gap-2 px-1 pt-1">
        {ev?.failureStats && ev.failureStats.length > 0 && (
          <Tag tone="mute">taxonomy: failure_taxonomy.ALL_FAILURE_CODES</Tag>
        )}
      </div>
    </div>
  );
}
