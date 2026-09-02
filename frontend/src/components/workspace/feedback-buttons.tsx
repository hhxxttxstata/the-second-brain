// 反馈按钮 — 每轮 Agent 回答后收集正奖励 / bad case（备注后回流 candidate）
import { useState } from "react";
import { Check, Loader2, ThumbsDown, ThumbsUp, X } from "lucide-react";
import { api } from "@/lib/workspace/api";

const BAD_TYPES = [
  { key: "useless", label: "回答没用" },
  { key: "tool_wrong", label: "工具调用错" },
  { key: "memory_wrong", label: "记忆写错" },
] as const;

type BadType = (typeof BAD_TYPES)[number]["key"];

export function FeedbackButtons({
  traceId,
  inputText,
}: {
  traceId: string;
  inputText: string;
}) {
  const [phase, setPhase] = useState<"idle" | "form" | "sending" | "done">("idle");
  const [badType, setBadType] = useState<BadType>("useless");
  const [note, setNote] = useState("");
  const [result, setResult] = useState("");

  const submitUseful = () => submit("useful", false);

  const submitBadCase = () => submit(badType, true);

  const submit = async (failureType: string, withNote: boolean) => {
    setPhase("sending");
    try {
      const resp = await api.sendFeedback({
        trace_id: traceId,
        failure_type: failureType,
        input: inputText,
        note: withNote ? note : "",
      });
      setResult(
        failureType === "useful"
          ? "正奖励已记录"
          : resp.candidate_id
            ? `已回流 candidate ${resp.candidate_id}`
            : "已记录（该 case 已在 candidate 池）",
      );
      setPhase("done");
      // 通知 shell 刷新快照，Evaluation 抽屉立即看到新 candidate
      window.dispatchEvent(new CustomEvent("ws:feedback-submitted"));
    } catch {
      setResult("提交失败，请重试");
      setPhase("form");
    }
  };

  if (phase === "done") {
    return (
      <p className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-ok">
        <Check size={11} />
        {result}
      </p>
    );
  }

  if (phase === "form") {
    return (
      <div className="mt-2 rounded-md border border-edge-soft bg-base-850 p-2.5">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-warn">
            Bad Case 反馈
          </span>
          <button
            onClick={() => setPhase("idle")}
            className="rounded p-0.5 text-faint hover:text-ink"
            title="取消"
          >
            <X size={12} />
          </button>
        </div>
        <div className="mb-1.5 flex flex-wrap gap-1">
          {BAD_TYPES.map((t) => (
            <button
              key={t.key}
              onClick={() => setBadType(t.key)}
              className={`rounded px-2 py-0.5 text-[11px] transition-colors ${
                badType === t.key
                  ? "bg-warn-soft text-warn"
                  : "bg-base-800 text-mute hover:text-ink"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={2}
          placeholder="补充说明（可选）— 期望的行为 / 实际哪里不对…"
          className="w-full resize-none rounded border border-edge bg-base-900 px-2 py-1.5 text-xs text-ink outline-none placeholder:text-faint focus:border-accent/60"
        />
        <div className="mt-1.5 flex items-center justify-between">
          <span className="font-mono text-[10px] text-faint">
            提交后自动回流 eval/candidate → 经 regression 验证后晋升
          </span>
          <button
            onClick={submitBadCase}
            className="flex items-center gap-1 rounded bg-warn px-2.5 py-1 text-[11px] font-medium text-base-950"
          >
            提交反馈
          </button>
        </div>
      </div>
    );
  }

  if (phase === "sending") {
    return (
      <p className="mt-1.5 inline-flex items-center gap-1.5 text-[11px] text-mute">
        <Loader2 size={11} className="animate-spin" /> 提交中…
      </p>
    );
  }

  return (
    <div className="mt-1.5 flex items-center gap-1.5">
      <button
        onClick={submitUseful}
        className="inline-flex items-center gap-1 rounded border border-edge-soft bg-base-850 px-2 py-0.5 text-[11px] text-mute transition-colors hover:border-ok/40 hover:text-ok"
        title="正奖励 — 记录为有效回答"
      >
        <ThumbsUp size={11} />
        正奖励
      </button>
      <button
        onClick={() => setPhase("form")}
        className="inline-flex items-center gap-1 rounded border border-edge-soft bg-base-850 px-2 py-0.5 text-[11px] text-mute transition-colors hover:border-warn/40 hover:text-warn"
        title="标记 bad case — 备注后回流 candidate"
      >
        <ThumbsDown size={11} />
        Bad case
      </button>
    </div>
  );
}
