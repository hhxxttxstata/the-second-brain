// Agent Timeline — 纯 CSS 时间线，每项可展开查看 Input/Output（不展示 CoT）
import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { AgentRun, AgentStep } from "@/lib/workspace/types";
import { agentLabel } from "@/lib/workspace/adapters";
import { JsonPreview, Spinner, Tag } from "./ui";

function StepDot({ status }: { status: AgentStep["status"] }) {
  const cls =
    status === "success"
      ? "border-ok bg-ok/20 text-ok"
      : status === "failed"
        ? "border-err bg-err/20 text-err"
        : status === "running"
          ? "border-run bg-run/20"
          : "border-faint bg-transparent";
  const inner =
    status === "success" ? (
      <span className="text-[9px] leading-none">✓</span>
    ) : status === "failed" ? (
      <span className="text-[9px] leading-none">✕</span>
    ) : status === "running" ? (
      <Spinner className="h-2 w-2" />
    ) : null;
  return (
    <span className={`flex h-4 w-4 items-center justify-center rounded-full border ${cls}`}>
      {inner}
    </span>
  );
}

function TimelineRow({ step, last }: { step: AgentStep; last: boolean }) {
  const [open, setOpen] = useState(false);
  const hasDetail = step.input != null || step.output != null || step.detail || step.action;

  return (
    <li className="relative flex gap-2.5 pb-3">
      {/* connector line */}
      {!last && <span className="absolute left-[7.5px] top-5 h-full w-px bg-edge" />}
      <div className="z-10 mt-0.5">
        <StepDot status={step.status} />
      </div>
      <div className="min-w-0 flex-1">
        <button
          onClick={() => hasDetail && setOpen((v) => !v)}
          className="flex w-full items-center gap-1.5 text-left"
        >
          {hasDetail ? (
            open ? (
              <ChevronDown size={11} className="shrink-0 text-faint" />
            ) : (
              <ChevronRight size={11} className="shrink-0 text-faint" />
            )
          ) : (
            <span className="w-[11px] shrink-0" />
          )}
          <span
            className={`text-xs font-medium ${
              step.status === "failed" ? "text-err" : step.status === "pending" ? "text-faint" : "text-ink"
            }`}
          >
            {step.label}
          </span>
          {step.durationMs != null && (
            <span className="font-mono text-[10px] text-faint">
              {step.durationMs < 1000 ? `${Math.round(step.durationMs)} ms` : `${(step.durationMs / 1000).toFixed(1)} s`}
            </span>
          )}
          {step.errorCode && <Tag tone="err">{step.errorCode}</Tag>}
        </button>
        {step.action && !open && (
          <p className="ml-[15px] truncate text-[11px] text-mute">{step.action}</p>
        )}
        {open && (
          <div className="ml-[15px] mt-1.5 space-y-2 rounded border border-edge-soft bg-base-850 p-2">
            <Field label="Agent" value={agentLabel(step.kind)} />
            {step.action && <Field label="Action" value={step.action} />}
            <Field label="Status" value={step.status} />
            {step.durationMs != null && <Field label="Duration" value={`${Math.round(step.durationMs)} ms`} />}
            {step.errorCode && <Field label="Error" value={step.errorCode} />}
            {step.detail && <Field label="Detail" value={step.detail} />}
            {step.input != null && (
              <div>
                <p className="mb-1 text-[10px] uppercase tracking-wider text-faint">Input</p>
                <JsonPreview data={step.input} />
              </div>
            )}
            {step.output != null && (
              <div>
                <p className="mb-1 text-[10px] uppercase tracking-wider text-faint">Output</p>
                <JsonPreview data={step.output} />
              </div>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="w-14 shrink-0 text-faint">{label}</span>
      <span className="min-w-0 flex-1 break-words font-mono text-mute">{value}</span>
    </div>
  );
}

export function AgentTimeline({ run }: { run: AgentRun }) {
  return (
    <ul className="mt-1">
      {run.steps.map((s, i) => (
        <TimelineRow key={s.id} step={s} last={i === run.steps.length - 1} />
      ))}
    </ul>
  );
}
