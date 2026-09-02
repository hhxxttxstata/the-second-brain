// Evaluation Tab — Eval Pulse + EvolutionLoop + Regression + Recent Candidates
import { useState } from "react";
import { FlaskConical, ShieldCheck, Waves } from "lucide-react";
import type { EvaluationSnapshot } from "@/lib/workspace/types";
import { EvolutionLoop, type EvolutionStage } from "./evolution-loop";
import { EmptyState, SectionTitle, StatusBadge, Tag } from "./ui";

function Pulse({
  icon,
  label,
  passed,
  total,
  ok,
}: {
  icon: React.ReactNode;
  label: string;
  passed: number | null;
  total: number | null;
  ok: boolean;
}) {
  return (
    <div className="rounded-md border border-edge-soft bg-base-850 px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-faint">
        {icon}
        <span className="text-[10px] uppercase tracking-wider">{label}</span>
      </div>
      <p className={`mt-1 font-mono text-lg ${ok ? "text-ok" : "text-ink"}`}>
        {passed != null && total != null ? `${passed}/${total}` : "—"}
      </p>
    </div>
  );
}

export function EvaluationPanel({ ev }: { ev: EvaluationSnapshot | null }) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (!ev) {
    return (
      <EmptyState
        title="Evaluation snapshot unavailable"
        hint="Last known baseline is preserved."
      />
    );
  }

  // 当前自进化阶段：有 candidate → candidate 节点；回归通过 → promoted
  const stage: EvolutionStage =
    ev.candidates.length > 0 ? "candidate" : ev.latestRegression?.status === "pass" ? "promoted" : "trace";

  return (
    <div className="space-y-4">
      {/* Eval Pulse */}
      <section className="space-y-1.5">
        <SectionTitle>Evaluation Pulse</SectionTitle>
        <div className="grid grid-cols-4 gap-2">
          <Pulse
            icon={<FlaskConical size={11} />}
            label="Golden"
            passed={ev.golden?.passed ?? null}
            total={ev.golden?.total ?? null}
            ok={!!ev.golden && ev.golden.passed === ev.golden.total}
          />
          <Pulse
            icon={<ShieldCheck size={11} />}
            label="Safety"
            passed={ev.safety?.passed ?? null}
            total={ev.safety?.total ?? null}
            ok={!!ev.safety}
          />
          <Pulse
            icon={<Waves size={11} />}
            label="Multi-turn"
            passed={ev.multiTurn?.passed ?? null}
            total={ev.multiTurn?.total ?? null}
            ok={!!ev.multiTurn && ev.multiTurn.passed === ev.multiTurn.total}
          />
          <div className="rounded-md border border-edge-soft bg-base-850 px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-faint">
              <span className="text-[10px] uppercase tracking-wider">Traceability</span>
            </div>
            <p className="mt-1 font-mono text-lg text-ink">
              {ev.traceability != null ? `${ev.traceability}%` : "—"}
            </p>
          </div>
        </div>
        {ev.totalScore != null && (
          <p className="px-1 font-mono text-[10px] text-faint">
            Scorecard total: {ev.totalScore} / 100 · source: scorecard L1-L8
          </p>
        )}
      </section>

      {/* Evolution Loop — 这个组件负责对面试官解释“自进化”是什么 */}
      <section className="space-y-1.5">
        <SectionTitle>Evolution Loop</SectionTitle>
        <EvolutionLoop current={stage} />
        <p className="px-1 text-[11px] leading-relaxed text-faint">
          自进化不是自动改代码，而是 eval-driven controlled evolution：bad case 固化为 candidate，经 regression 验证后才晋升进 baseline。
        </p>
      </section>

      {/* Latest Regression */}
      <section className="space-y-1.5">
        <SectionTitle>Latest Regression</SectionTitle>
        {ev.latestRegression ? (
          <div className="flex items-center gap-3 rounded-md border border-edge-soft bg-base-850 px-3 py-2">
            <StatusBadge tone={ev.latestRegression.status === "pass" ? "ok" : "err"}>
              {ev.latestRegression.status.toUpperCase()}
            </StatusBadge>
            <span className="font-mono text-[11px] text-mute">
              gate: {ev.latestRegression.promotionGate ?? "—"}
              {ev.latestRegression.p95DeltaPct != null && ` · p95 Δ ${ev.latestRegression.p95DeltaPct}%`}
            </span>
            <span className="flex-1" />
            <span className="font-mono text-[10px] text-faint">{ev.latestRegression.at ?? ""}</span>
          </div>
        ) : (
          <p className="px-1 text-[11px] text-faint">尚无回归记录 — 运行 benchmark 后生成。</p>
        )}
      </section>

      {/* Recent Candidates */}
      <section className="space-y-1.5">
        <SectionTitle
          right={<span className="font-mono text-[10px] text-faint">{ev.candidates.length} candidates</span>}
        >
          Recent Candidates
        </SectionTitle>
        {ev.candidates.length === 0 ? (
          <p className="px-1 text-[11px] text-faint">No candidates — 当前无待修复 bad case。</p>
        ) : (
          <div className="space-y-1">
            {ev.candidates.map((c) => {
              const open = expanded === c.id;
              return (
                <div key={c.id} className="rounded-md border border-edge-soft bg-base-850">
                  <button
                    onClick={() => setExpanded(open ? null : c.id)}
                    className="flex w-full items-center gap-2 px-2.5 py-2 text-left"
                  >
                    <span className="font-mono text-xs text-run">{c.id}</span>
                    <span className="min-w-0 flex-1 truncate text-xs text-ink">{c.intent || c.failureType}</span>
                    <Tag tone="warn">{c.failureType}</Tag>
                    <StatusBadge tone="run">{c.status}</StatusBadge>
                  </button>
                  {open && (
                    <div className="space-y-1.5 border-t border-edge-soft px-2.5 py-2 text-[11px]">
                      <Row label="Failure Type" value={c.failureType} />
                      <Row label="Expected Route" value={c.expectedRoute ?? "—"} />
                      <Row label="Known Issue" value={c.knownIssue ?? "—"} />
                      <Row label="Trace" value={c.traceRef ?? "—"} />
                      <Row label="Next" value="awaiting fix → regression → promotion" />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <span className="w-24 shrink-0 text-faint">{label}</span>
      <span className="min-w-0 flex-1 break-words font-mono text-mute">{value}</span>
    </div>
  );
}
