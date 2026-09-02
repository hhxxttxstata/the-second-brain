// 会话切换器 — Conversation Header 左侧的历史会话下拉：
// 活跃会话列表 / 新建 / 归档；归档列表可恢复或彻底删除。
// 历史会话持久化在 localStorage，刷新不丢失，仅归档后从主列表移除。
import { useState } from "react";
import {
  Archive,
  ArchiveRestore,
  ChevronDown,
  History,
  MessageSquare,
  Plus,
  Trash2,
} from "lucide-react";
import type { StoredSession } from "@/lib/workspace/session-store";
import { timeAgo } from "@/lib/workspace/adapters";

export function SessionSwitcher({
  sessions,
  activeId,
  disabled,
  onSelect,
  onNew,
  onArchive,
  onUnarchive,
  onDelete,
}: {
  sessions: StoredSession[];
  activeId: string | null;
  disabled?: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onArchive: (id: string) => void;
  onUnarchive: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const active = sessions.find((s) => s.id === activeId) ?? null;
  const visible = sessions.filter((s) => s.archived === showArchived);

  const pick = (id: string) => {
    onSelect(id);
    setOpen(false);
  };

  return (
    <div className="relative min-w-0">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        title="历史会话（本地保存，刷新不丢失）"
        className="flex max-w-[260px] items-center gap-1.5 rounded border border-edge-soft bg-base-850 px-2 py-1 transition-colors hover:border-base-750 disabled:opacity-50"
      >
        <History size={12} className="shrink-0 text-faint" />
        <span className="truncate text-xs font-semibold text-ink">{active?.title ?? "新会话"}</span>
        <ChevronDown size={12} className={`shrink-0 text-faint transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <>
          {/* 透明遮罩：点击下拉外部关闭 */}
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-full z-40 mt-1.5 w-80 rounded-lg border border-edge bg-base-900 shadow-xl shadow-black/40">
            {/* tabs + 新建 */}
            <div className="flex items-center gap-1 border-b border-edge px-2 py-1.5">
              <button
                onClick={() => setShowArchived(false)}
                className={`rounded px-2 py-0.5 text-[11px] font-medium transition-colors ${
                  !showArchived ? "bg-base-800 text-ink" : "text-mute hover:text-ink"
                }`}
              >
                会话
              </button>
              <button
                onClick={() => setShowArchived(true)}
                className={`flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium transition-colors ${
                  showArchived ? "bg-base-800 text-ink" : "text-mute hover:text-ink"
                }`}
              >
                <Archive size={10} />
                归档
              </button>
              <div className="flex-1" />
              <button
                onClick={() => {
                  onNew();
                  setOpen(false);
                }}
                disabled={disabled}
                className="flex items-center gap-1 rounded bg-accent px-2 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                <Plus size={11} />
                新会话
              </button>
            </div>

            {/* 会话列表 */}
            <div className="max-h-72 overflow-y-auto p-1">
              {visible.length === 0 ? (
                <p className="px-3 py-6 text-center text-[11px] text-faint">
                  {showArchived ? "暂无归档会话 — 归档后可在「归档」中恢复" : "暂无历史会话"}
                </p>
              ) : (
                visible.map((s) => {
                  const isActive = s.id === activeId;
                  const assistantCount = s.messages.filter((m) => m.role === "assistant").length;
                  return (
                    <div
                      key={s.id}
                      onClick={() => !s.archived && pick(s.id)}
                      className={`group flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 transition-colors ${
                        isActive ? "bg-base-800" : "hover:bg-base-850"
                      }`}
                    >
                      <MessageSquare size={12} className={isActive ? "shrink-0 text-accent" : "shrink-0 text-faint"} />
                      <div className="min-w-0 flex-1">
                        <p className={`truncate text-xs ${isActive ? "font-medium text-ink" : "text-mute"}`}>
                          {s.title}
                        </p>
                        <p className="font-mono text-[10px] text-faint">
                          {timeAgo(s.updatedAt) || "刚刚"} · {assistantCount} 轮
                          {s.archived ? " · 已归档" : ""}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                        {s.archived ? (
                          <>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                onUnarchive(s.id);
                              }}
                              title="恢复到会话列表"
                              className="rounded p-1 text-mute hover:bg-base-800 hover:text-ok"
                            >
                              <ArchiveRestore size={13} />
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                onDelete(s.id);
                              }}
                              title="彻底删除"
                              className="rounded p-1 text-mute hover:bg-base-800 hover:text-err"
                            >
                              <Trash2 size={13} />
                            </button>
                          </>
                        ) : (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              onArchive(s.id);
                            }}
                            title="归档（从列表移除，可在归档中恢复）"
                            className="rounded p-1 text-mute hover:bg-base-800 hover:text-warn"
                          >
                            <Archive size={13} />
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })
              )}
            </div>
            <p className="border-t border-edge px-3 py-1.5 font-mono text-[10px] text-faint">
              会话保存在浏览器本地 · 仅归档后从列表移除
            </p>
          </div>
        </>
      )}
    </div>
  );
}
