// 消息条目 — 用户/助手消息 + 助手消息下挂 Action Strip
import type { ChatMessage, AgentRun } from "@/lib/workspace/types";
import { ActionStrip } from "./action-strip";
import { Spinner } from "./ui";

export function MessageItem({
  message,
  run,
}: {
  message: ChatMessage;
  run?: AgentRun;
}) {
  const isUser = message.role === "user";
  const executing = run?.status === "running";

  return (
    <div className="px-4 py-2">
      <div className="mb-1 flex items-center gap-2">
        <span
          className={`text-[11px] font-semibold uppercase tracking-wider ${isUser ? "text-accent" : "text-ok"}`}
        >
          {isUser ? "You" : "Assistant"}
        </span>
        <span className="font-mono text-[10px] text-faint">{message.runId ?? ""}</span>
      </div>
      <div className={isUser ? "text-sm text-ink" : "text-sm text-ink/90"}>
        {message.content ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : executing ? (
          <p className="flex items-center gap-2 text-xs text-mute">
            <Spinner /> Agent 正在执行…
          </p>
        ) : (
          <p className="text-xs text-err">执行中断，未返回结果。</p>
        )}
      </div>
      {!isUser && run && run.status !== "idle" && <ActionStrip run={run} />}
    </div>
  );
}
