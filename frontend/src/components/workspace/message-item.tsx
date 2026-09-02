// 消息条目 — 用户/助手消息 + 助手消息下挂 Action Strip 与反馈按钮
import type { ChatMessage, AgentRun } from "@/lib/workspace/types";
import { ActionStrip } from "./action-strip";
import { FeedbackButtons } from "./feedback-buttons";
import { Spinner } from "./ui";

export function MessageItem({
  message,
  run,
  inputText,
}: {
  message: ChatMessage;
  run?: AgentRun;
  /** 该轮对应的用户原话（反馈 bad case 时回传给后端） */
  inputText?: string;
}) {
  const isUser = message.role === "user";
  const executing = run?.status === "running";
  // 只对真实完成的 run 开放反馈（demo/占位 run 没有 trace 可挂）
  const traceId =
    run && run.source === "live" && run.id.startsWith("trace_") && run.status !== "running"
      ? run.id
      : null;

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
      {!isUser && traceId && <FeedbackButtons traceId={traceId} inputText={inputText ?? ""} />}
    </div>
  );
}
