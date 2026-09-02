// Memory Tab — 三层记忆（Profile / Episodic / Task）+ 当前 Run 的 Memory Delta
import { useMemo, useState } from "react";
import type { MemoryDelta, MemoryLayer, MemoryRecord } from "@/lib/workspace/types";
import { timeAgo } from "@/lib/workspace/adapters";
import { EmptyState, SectionTitle, Tag } from "./ui";

const LAYERS: { key: MemoryLayer; label: string; hint: string }[] = [
  { key: "profile", label: "Profile", hint: "stable_profile" },
  { key: "episodic", label: "Episodic", hint: "episodic / conversation" },
  { key: "task", label: "Task", hint: "task / todos / plan" },
];

export function MemoryPanel({
  memories,
  deltas,
}: {
  memories: MemoryRecord[];
  deltas: MemoryDelta[];
}) {
  const [layer, setLayer] = useState<MemoryLayer>("episodic");
  const filtered = useMemo(() => memories.filter((m) => m.type === layer), [memories, layer]);

  return (
    <div className="grid h-full grid-cols-2 gap-4">
      {/* 三层记忆 */}
      <section className="flex min-h-0 flex-col gap-1.5">
        <SectionTitle>Memory Layers</SectionTitle>
        <div className="flex gap-1">
          {LAYERS.map((l) => (
            <button
              key={l.key}
              onClick={() => setLayer(l.key)}
              className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                layer === l.key
                  ? "bg-accent-soft text-accent"
                  : "bg-base-850 text-mute hover:text-ink"
              }`}
            >
              {l.label}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
          {filtered.length === 0 ? (
            <EmptyState title={`No ${layer} memories`} hint="该层暂无记忆记录。" />
          ) : (
            filtered.map((m) => (
              <div key={m.id} className="rounded-md border border-edge-soft bg-base-850 px-2.5 py-2">
                <p className="text-xs text-ink">{m.content}</p>
                <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-faint">
                  <Tag tone={m.type === "profile" ? "accent" : m.type === "episodic" ? "ok" : "warn"}>
                    {m.type}
                  </Tag>
                  {m.importance != null && <span>imp {m.importance}</span>}
                  {m.source && <span className="truncate">src: {m.source}</span>}
                  <span className="ml-auto shrink-0">{timeAgo(m.createdAt) || "—"}</span>
                </div>
              </div>
            ))
          )}
        </div>
      </section>

      {/* Memory Delta — 最近 run 的记忆变化，最能体现“自进化” */}
      <section className="flex min-h-0 flex-col gap-1.5">
        <SectionTitle>Memory Changes（最近 Run）</SectionTitle>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
          {deltas.length === 0 ? (
            <EmptyState title="No memory changes in this run." />
          ) : (
            deltas.map((d) => (
              <div key={d.id} className="rounded-md border border-edge-soft bg-base-850 px-2.5 py-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[10px] text-ok">+ Created</span>
                  <Tag tone={d.type === "profile" ? "accent" : d.type === "episodic" ? "ok" : "warn"}>
                    {d.type}
                  </Tag>
                  <span className="ml-auto font-mono text-[10px] text-faint">
                    run #{(d.sourceRunId ?? "").slice(0, 14)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-ink">{d.content}</p>
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
