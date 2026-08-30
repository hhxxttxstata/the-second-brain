"""Approval Router — 审批路由中间件（轻量版）。

连接 pending_ledger + chatbot system prompt，实现跨会话审批延续。

流程 (2026-08 简化，去掉执行状态机):
  1. Agent 提议操作 → propose_action() → pending_ledger
  2. build_context() 从 pending_ledger 拉取待审批摘要 → 注入 system prompt
  3. 用户说"同意" → approve_action() 标记批准；用户说"拒绝" → reject_action()
  4. 执行由模型在对话流完成（prompt 规则：用户确认后立即执行），
     不再由 ledger 编排 executing/executed 状态（execution_log 已停用）

匹配规则收紧（issue #10，降低误批准面）:
  - 拒绝词优先于同意词："不同意/不好/不可以" 含肯定词子串，必须先判否定
  - 疑问句（"可以吗？"）不是审批回复，不做任何裁决
  - 弱肯定词（好/是/可以/行）仅当整句是纯确认短语时生效；
    长句需要强肯定词（同意/批准/确认/允许/approve）
  - 部分批准：点名某个操作（"同意导出笔记"）只批准匹配项，其余保持待审批；
    全量词（全部/所有/都）才一次性批准全部
  - 每次批准/拒绝落盘 audit/approval_events.jsonl，供评分卡派生审批覆盖率
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .pending_ledger import (
    propose_action,
    approve_action,
    reject_action,
    get_pending_actions,
    get_pending_summary,
    get_pending_tasks_summary,
    init_ledger,
)

init_ledger()

# 当前会话正在等待审批的 key（用于跨会话延续）
_current_pending_keys: list[str] = []

# 审计事件目录（与 tool_registry 的 tool_calls.jsonl 同目录）
_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "agent_data" / "audit"

# 否定/取消词 —— 必须先于肯定词判定（"不同意"含"同意"子串）
_REJECT_PATTERNS = re.compile(
    r"(不同意|不对|不好|不行|不要|不准|不可以|不是|先不|暂不|别提|别执行|"
    r"不需要|不用了|拒绝|取消|撤回|算了吧|算了|cancel|reject)",
    re.I,
)

# 强肯定词 —— 出现在非否定句中任意位置即可批准
_APPROVE_STRONG = re.compile(r"(同意|批准|确认|允许|授权|approve|confirm|yes|ok\b)", re.I)

# 弱肯定词 —— 仅当整句是纯确认短语时生效（防止"这个方案好不好"误触发）
_APPROVE_BARE = re.compile(
    r"^\s*(好的?|是的?|可以|行|中|成|ok(?:ay)?|yes|sure|go)\s*[啊呢吧哦哈。.！!~\s]*$",
    re.I,
)

# 疑问句 —— 不做裁决（"可以吗？"是在提问，不是批准）
_QUESTION = re.compile(r"[?？]|吗\s*[。.！!]?\s*$|呢\s*[。.！!]?\s*$")

# 全量批准词
_SCOPE_ALL = ("全部", "所有", "都批准", "全批", "都同意", "都执行", "都通过")

# 描述分词时跳过的停用词（部分批准的实义词匹配用）
_STOPWORDS = {
    "一个", "这个", "那个", "操作", "执行", "需要", "帮我", "一下",
    "然后", "并且", "可以", "进行", "用户", "请求",
}


def get_current_pending_keys() -> list[str]:
    return _current_pending_keys


def clear_pending_keys() -> None:
    _current_pending_keys.clear()


def log_security_event(event: str, detail: dict[str, Any] | None = None) -> None:
    """审批/拒绝事件落盘 audit/approval_events.jsonl。

    与 tool_calls.jsonl 对齐，评分卡据此派生"高风险调用审批覆盖率"。
    """
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {"event": event, "time": datetime.now().isoformat()}
        if detail:
            record.update(detail)
        (_AUDIT_DIR / "approval_events.jsonl").open("a", encoding="utf-8").write(
            json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _select_pending_by_text(pending: list[dict[str, Any]],
                            text_lower: str) -> list[dict[str, Any]]:
    """从审批文本中匹配被点名的操作（部分批准用）。

    规则：pending 描述的实义词完整出现在文本中，或其 2-gram 命中 ≥2 处
    （容忍描述被改写/截断）；都不满足才不命中。
    """
    selected = []
    for action in pending:
        desc = str(action.get("description", ""))
        tokens = [t.lower() for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", desc)
                  if t not in _STOPWORDS]
        full_hit = any(t in text_lower for t in tokens)
        grams: set[str] = set()
        for t in tokens:
            grams.update(t[i:i + 2] for i in range(len(t) - 1))
        gram_hits = sum(1 for g in grams if g in text_lower)
        if full_hit or gram_hits >= 2:
            selected.append(action)
    return selected


def route_approval(text: str, session_id: str, turn_id: str = "") -> str | None:
    """解析用户的审批意图。

    Returns:
      "approved" | "approved_partial" | "rejected" | None (not an approval response)
    """
    text_lower = text.strip().lower()
    if not text_lower:
        return None

    # 检查是否在对 pending action 做回应
    pending = get_pending_actions(session_id=session_id, status="pending_approval")
    if not pending:
        return None

    # 疑问句不是审批回复（"可以吗？"是在提问）——不裁决
    if _QUESTION.search(text_lower):
        return None

    # 1. 拒绝优先："不同意/不好/不可以" 含肯定词子串，先判否定才不会误批准
    if _REJECT_PATTERNS.search(text_lower):
        for action in pending:
            reject_action(action["idempotency_key"])
        log_security_event("rejection", {
            "session_id": session_id,
            "text": text[:80],
            "rejected_count": len(pending),
        })
        return "rejected"

    # 2. 肯定判定：强肯定词任意位置命中，或整句是纯确认短语（弱肯定词）
    strong = bool(_APPROVE_STRONG.search(text_lower))
    bare = bool(_APPROVE_BARE.match(text_lower))
    if not (strong or bare):
        return None

    # 3. 范围限定：点名批准部分操作 → 只批匹配项（降低一次性批准全部的误批准面）
    scope_all = any(kw in text_lower for kw in _SCOPE_ALL)
    selected = _select_pending_by_text(pending, text_lower)
    if not scope_all and selected and len(selected) < len(pending):
        for action in selected:
            approve_action(action["idempotency_key"])
            if action["idempotency_key"] not in _current_pending_keys:
                _current_pending_keys.append(action["idempotency_key"])
        log_security_event("approval_partial", {
            "session_id": session_id,
            "text": text[:80],
            "approved_count": len(selected),
            "remaining": len(pending) - len(selected),
        })
        return "approved_partial"

    # 4. 全量批准
    for action in pending:
        approve_action(action["idempotency_key"])
        if action["idempotency_key"] not in _current_pending_keys:
            _current_pending_keys.append(action["idempotency_key"])
    log_security_event("approval", {
        "session_id": session_id,
        "text": text[:80],
        "approved_count": len(pending),
    })
    return "approved"


def inject_pending_context() -> str:
    """构建 pending context 字符串（供 system prompt 注入）。"""
    parts = []
    pending = get_pending_actions(status="pending_approval")
    if pending:
        lines = ["\n## 待审批操作"]
        for a in pending:
            lines.append(f"  - [{a['action_type']}] {a['description']}")
            lines.append(f"    回复「同意」来批准执行")
        parts.append("\n".join(lines))

    approved = get_pending_actions(status="approved")
    if approved:
        lines = ["\n## 已批准待执行"]
        for a in approved:
            lines.append(f"  - [{a['action_type']}] {a['description']}")
        parts.append("\n".join(lines))

    return "\n".join(parts)
