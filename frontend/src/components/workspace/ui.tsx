// 工作台小型共享原语：徽章 / 空状态 / 区块标题 / 加载态
import type { ReactNode } from "react";

const TONES = {
  ok: { text: "text-ok", bg: "bg-ok-soft", dot: "bg-ok" },
  warn: { text: "text-warn", bg: "bg-warn-soft", dot: "bg-warn" },
  err: { text: "text-err", bg: "bg-err-soft", dot: "bg-err" },
  run: { text: "text-run", bg: "bg-run-soft", dot: "bg-run" },
  accent: { text: "text-accent", bg: "bg-accent-soft", dot: "bg-accent" },
  mute: { text: "text-mute", bg: "bg-base-800", dot: "bg-faint" },
} as const;

export type Tone = keyof typeof TONES;

export function StatusBadge({
  tone,
  children,
  pulse = false,
}: {
  tone: Tone;
  children: ReactNode;
  pulse?: boolean;
}) {
  const t = TONES[tone];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-xs font-medium ${t.bg} ${t.text}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${t.dot} ${pulse ? "animate-pulse" : ""}`} />
      {children}
    </span>
  );
}

export function Tag({ tone = "mute", children }: { tone?: Tone; children: ReactNode }) {
  const t = TONES[tone];
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-px font-mono text-[10px] ${t.bg} ${t.text}`}>
      {children}
    </span>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between px-1">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">{children}</h3>
      {right}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  icon,
}: {
  title: string;
  hint?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 px-4 py-8 text-center">
      {icon && <div className="mb-1 text-faint">{icon}</div>}
      <p className="text-xs font-medium text-mute">{title}</p>
      {hint && <p className="text-[11px] text-faint">{hint}</p>}
    </div>
  );
}

export function Card({
  children,
  className = "",
  onClick,
}: {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
}) {
  return (
    <div
      onClick={onClick}
      className={`rounded-md border border-edge bg-base-900 ${onClick ? "cursor-pointer transition-colors hover:border-base-750 hover:bg-base-850" : ""} ${className}`}
    >
      {children}
    </div>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3 w-3 animate-spin rounded-full border-[1.5px] border-faint border-t-accent ${className}`}
    />
  );
}

export function JsonPreview({ data, max = 6 }: { data: unknown; max?: number }) {
  let text: string;
  try {
    text = JSON.stringify(data, null, 2) ?? "null";
  } catch {
    text = String(data);
  }
  const lines = text.split("\n");
  const trimmed = lines.length > max ? [...lines.slice(0, max), "…"].join("\n") : text;
  return (
    <pre className="overflow-x-auto rounded bg-base-950 p-2 font-mono text-[11px] leading-relaxed text-mute">
      {trimmed}
    </pre>
  );
}
