// EvolutionLoop — 自进化闭环可视化：Trace → Failure → Candidate → Regression → Promoted
// 不画流程图，5 个横向 step，当前节点高亮（交接文档 §13）
import { Check, Circle } from "lucide-react";

export type EvolutionStage = "trace" | "failure" | "candidate" | "regression" | "promoted";

const STAGES: { key: EvolutionStage; label: string }[] = [
  { key: "trace", label: "Trace" },
  { key: "failure", label: "Failure" },
  { key: "candidate", label: "Candidate" },
  { key: "regression", label: "Regression" },
  { key: "promoted", label: "Promoted" },
];

export function EvolutionLoop({ current }: { current: EvolutionStage }) {
  const currentIndex = STAGES.findIndex((s) => s.key === current);

  return (
    <div className="flex items-center justify-between rounded-md border border-edge-soft bg-base-850 px-4 py-3">
      {STAGES.map((stage, i) => {
        const done = i < currentIndex;
        const active = i === currentIndex;
        return (
          <div key={stage.key} className="flex flex-1 items-center last:flex-none">
            <div className="flex flex-col items-center gap-1">
              <span
                className={`flex h-6 w-6 items-center justify-center rounded-full border ${
                  done
                    ? "border-ok bg-ok/15 text-ok"
                    : active
                      ? "border-accent bg-accent/15 text-accent"
                      : "border-edge bg-base-900 text-faint"
                }`}
              >
                {done ? (
                  <Check size={12} />
                ) : (
                  <Circle size={active ? 10 : 6} className={active ? "fill-accent/40" : ""} />
                )}
              </span>
              <span
                className={`text-[10px] font-medium ${
                  active ? "text-accent" : done ? "text-ok" : "text-faint"
                }`}
              >
                {stage.label}
              </span>
            </div>
            {i < STAGES.length - 1 && (
              <span
                className={`mx-2 mb-4 h-px flex-1 ${done ? "bg-ok/50" : "bg-edge"}`}
                aria-hidden
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
