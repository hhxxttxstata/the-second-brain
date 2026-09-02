"""CLI entry point for Agentic Data Platform.

Usage:
    python -m app.cli plan              Generate daily plan
    python -m app.cli steward           Run data steward audit
    python -m app.cli search <query>    Search knowledge base
    python -m app.cli ingest <path>     Ingest a file
    python -m app.cli scan              Scan knowledge directory
    python -m app.cli ask <question>    Ask a question (context + plan)
    python -m app.cli status            Show system status
"""

from __future__ import annotations

import json
import sys
from datetime import date

import requests

# Try common ports for the running server
import os
_PORT = os.environ.get("API_PORT", "8000")
API_BASE = f"http://127.0.0.1:{_PORT}"


def _detect_port() -> str:
    """Try to find the running server."""
    for port in ["8000", "8011", "8010", "8009", "8008"]:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", int(port)))
            s.close()
            try:
                import requests
                r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
                if r.status_code == 200:
                    return port
            except Exception:
                pass
        except Exception:
            pass
    return "8000"  # default fallback


API_BASE = f"http://127.0.0.1:{_detect_port()}"


def _post(path: str, data: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    try:
        resp = requests.post(url, json=data or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        print(f"❌ 无法连接服务器 ({API_BASE}/health)")
        print("   请先启动:  uvicorn app.main:app --reload")
        sys.exit(1)
    except Exception as e:
        return {"error": str(e)}


def _get(path: str) -> dict:
    url = f"{API_BASE}{path}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        print(f"❌ 无法连接服务器 ({API_BASE}/health)")
        print("   请先启动:  uvicorn app.main:app --reload")
        sys.exit(1)


def cmd_plan():
    """生成今日计划"""
    print("🤖 正在生成今日计划...")
    result = _post("/agent/daily-plan")
    if not result.get("success"):
        print(f"❌ 失败: {result.get('error', '未知错误')}")
        return

    items = result.get("items", [])
    print(f"\n📋 今日计划 ({result.get('date', date.today().isoformat())})")
    print(f"   共 {len(items)} 项，基于 {result.get('context_count', 0)} 个数据资产\n")

    for i, item in enumerate(items, 1):
        src = item.get("source", "")
        icon = {"diary_todo": "📓", "pending_task": "🔄", "signal": "📡",
                "stable_profile": "🎯", "default": "📝"}.get(src, "•")
        print(f"  {i}. {icon} [{item.get('priority', 'medium').upper()}] {item.get('title', '')}")
        if item.get("description"):
            print(f"     {item['description'][:80]}")

    print(f"\n   ⏱  {result.get('latency_ms', 0)}ms")

    if result.get("plan_id"):
        print(f"\n💡 采纳这条计划:  curl -X POST {API_BASE}/agent/adopt-plan"
              f" -H 'Content-Type: application/json'"
              f" -d '{{\"plan_id\": \"{result['plan_id']}\"}}'")


def cmd_steward():
    """数据资产巡检"""
    print("🔍 正在巡检数据资产...")
    result = _post("/agent/data-steward")
    if not result.get("success"):
        print(f"❌ 失败: {result.get('error', '未知错误')}")
        return

    findings = result.get("findings", [])
    print(f"\n🛡️  Data Steward 巡检报告 ({result.get('date', date.today().isoformat())})")
    print(f"   总资产: {result.get('total_assets', 0)} | 发现项: {result.get('total_findings', 0)}")

    severity_icons = {"high": "🔴", "medium": "🟡", "low": "🟢", "info": "ℹ️"}

    # Group by severity
    for sev in ["high", "medium", "low", "info"]:
        sev_items = [f for f in findings if f.get("severity") == sev][:5]
        if not sev_items:
            continue
        icon = severity_icons.get(sev, "•")
        print(f"\n   {icon} {sev.upper()}")
        for f in sev_items:
            detail = f.get("detail", "")[:80]
            print(f"     [{f.get('type', '?')}] {detail}")

    other = len(findings) - sum(1 for f in findings if f.get("severity") in severity_icons)
    if other > 0:
        print(f"\n   ...及其他 {other} 项")

    print(f"\n   ⏱  {result.get('latency_ms', 0)}ms")


def cmd_search(query: str):
    """搜索知识库"""
    if not query:
        print("❌ 请输入搜索关键词")
        print("   用法: python -m app.cli search <关键词>")
        return

    print(f"🔎 正在搜索: {query}")
    result = _post("/knowledge/search", {"query": query, "top_k": 5})
    print("DEBUG result keys:", list(result.keys()))
    print("DEBUG total:", result.get("total", "MISSING"))
    items = result.get("results", [])
    print(f"\n📚 搜索结果 ({len(items)} 条)\n")

    for i, r in enumerate(items, 1):
        print(f"  {i}. [{r.get('score', 0):.3f}] {r.get('text', '')[:80]}...")
        print(f"     📁 {r.get('source_file', '')}")
        if r.get("heading"):
            print(f"     📎 {r['heading']}")
        print()


def cmd_ingest(path: str):
    """接入一个文件"""
    if not path:
        print("❌ 请指定文件路径")
        print("   用法: python -m app.cli ingest <文件路径>")
        return

    print(f"📥 正在接入: {path}")
    result = _post("/knowledge/ingest", {"file_path": path})

    if "error" in result:
        print(f"❌ 失败: {result['error']}")
        return

    r = result.get("result", {})
    print(f"   raw: {r.get('raw_asset_id', 'N/A')}")
    print(f"   clean: {r.get('clean_asset_id', 'N/A')}")
    print(f"   ingested: {r.get('ingested_count', 0)} | skipped: {r.get('skipped_count', 0)}")
    if r.get("ingested_count", 0) > 0:
        print(f"   ✅ 接入完成")
    else:
        print(f"   ⏭️  跳过（已存在）")


def cmd_scan():
    """全量扫描知识库"""
    print("📂 正在扫描知识目录...")
    result = _post("/knowledge/scan")

    total = result.get("total_files", 0)
    ingested = result.get("ingested", 0)
    print(f"\n   扫描文件: {total}")
    print(f"   新接入: {ingested}")
    if result.get("errors"):
        print(f"   错误: {len(result['errors'])}")
        for e in result["errors"][:3]:
            print(f"     ⚠️  {e.get('error', str(e))[:80]}")
    print(f"   ✅ 扫描完成")


def cmd_ask(question: str):
    """向系统提问（走 Orchestrator 自动路由，本地调用 - 无需启动服务器）"""
    if not question:
        print("❌ 请输入问题")
        print("   用法: python -m app.cli ask <你的问题>")
        return

    print(f"💬 Orchestrator 处理中: {question}")
    print()

    from app.agent.graphs.orchestrator import run_orchestrator
    from app.agent.session import get_default_session, load_messages, append_exchange
    from app.agent.approval_router import route_approval
    from app.agent.session_jsonl import log_session_summary

    session_id = get_default_session()
    history = load_messages(session_id)

    # 检查是否有待审批操作
    from app.agent.pending_ledger import get_pending_actions
    pending = get_pending_actions(session_id=session_id, status="pending_approval")
    if pending:
        approval = route_approval(question, session_id=session_id)
        if approval == "approved":
            print("  ✅ 已批准，继续执行...")
        elif approval == "approved_partial":
            remaining = get_pending_actions(session_id=session_id, status="pending_approval")
            print(f"  ✅ 已批准被点名的操作，剩余 {len(remaining)} 条仍待审批")
        elif approval == "rejected":
            print("  ❌ 已拒绝")

    result = run_orchestrator(
        input_text=question,
        thread_id=session_id,
        conversation=history,
    )

    if result.get("success"):
        route = result.get("route", "?")
        print(f"  路由: {route} ({result.get('route_reason', '')})")
        print(f"  ⏱  {result.get('latency_ms', 0)}ms")
        print()
        if result.get("result"):
            print(result["result"])
        if result.get("result_data"):
            print(f"\n  (data: {result['result_data']})")

        if result.get("result"):
            append_exchange(session_id, question, result["result"])

        # 写 session summary 到 JSONL（语义化：工具轨迹 + LLM 提炼 + handoff 关联）
        trace = None
        try:
            from app.agent.trace import get_latest_trace
            trace = get_latest_trace()
        except Exception:
            pass
        from app.agent.session_jsonl import log_session_summary_semantic
        log_session_summary_semantic(
            session_id=session_id,
            question=question,
            answer=result.get("result", ""),
            route=result.get("route", "?"),
            trace=trace,
            trace_id=result.get("trace_id", "") or result.get("run_id", ""),
        )
    else:
        print(f"❌ 失败: {result.get('error', '未知错误')}")


def _cmd_ui():
    """启动终端 UI（已弃用 — webui 已归档，请使用 chat_web.py / chat.py）"""
    print("⚠️ 终端 UI (webui) 已弃用并归档。\n"
          "   主入口: streamlit run app/chat_web.py\n"
          "   调试入口: python -X utf8 -m app.chat")


def cmd_status():
    """系统状态"""
    print("📊 Agentic Data Platform — 状态\n")

    # Health
    health = _get("/health")
    print(f"  Server: {health.get('status', 'unknown')}")
    print(f"  Version: {health.get('version', '?')}")

    # Metrics
    metrics = _get("/observability/metrics")
    a = metrics.get("asset_metrics", {})
    ag = metrics.get("agent_metrics", {})
    g = metrics.get("governance_metrics", {})

    print(f"\n📦 数据资产")
    print(f"  总资产: {a.get('total_assets', '?')}")
    print(f"  平均质量分: {a.get('avg_quality_score', '?')}")
    print(f"  高质量占比: {a.get('high_quality_ratio', '?')}%")
    print(f"  过期资产: {a.get('expired_count', '?')}")

    print(f"\n🤖 Agent")
    print(f"  累计调用: {ag.get('total_traces', '?')}")
    print(f"  工具成功率: {ag.get('tool_success_rate', '?')}%")
    print(f"  上下文命中率: {ag.get('context_hit_rate', '?')}%")
    print(f"  计划采纳率: {ag.get('plan_adoption_rate', '?')}%")

    print(f"\n🛡️  治理")
    print(f"  血缘完整率: {g.get('lineage_completeness', '?')}%")
    print(f"  Steward 报告数: {g.get('steward_reports_generated', '?')}")

    print(f"\n📎 Dashboard: {API_BASE}/observability/dashboard")


def cmd_evolve():
    """自进化：蒸馏 trace → 生成/验证策略 → A/B 门 → 固化 skill"""
    args = sys.argv[2:]
    if args and args[0] == "status":
        _evolve_status()
        return
    if args and args[0] == "meta":
        _evolve_meta()
        return
    if args and args[0] == "promote":
        _evolve_promote(args[1:])
        return
    if args and args[0] == "rollback":
        _evolve_rollback(args[1:])
        return
    dry_run = "--dry-run" in args
    force = "--force" in args

    from app.agent.evolution.runner import evolve_now
    from app.agent.evolution import experience as exp

    print("🧬 自进化闭环启动\n")
    s = exp.status_summary()
    print(f"  Trace 总数: {s['trace_count']}（未蒸馏: {s['undistilled_count']}）")
    print(f"  上次蒸馏: {s['last_distilled_at'][:16]}")
    print(f"  累计蒸馏: {s['distill_count']} 次, 自动进化: {s['auto_evolve_count']} 次\n")

    if dry_run:
        print("  [dry-run] 只做数据准备，不调用 LLM、不写入\n")
    report = evolve_now(force=force, dry_run=dry_run)

    d = report.get("distill", {})
    if d.get("skipped"):
        print(f"  ⏭️  蒸馏跳过: {d.get('reason')}")
    elif d.get("dry_run"):
        print(f"  🔬 [dry-run] 蒸馏批: {d.get('batch_size')} 条 trace")
        print(f"     prompt 预览:\n{d.get('prompt_preview', '')[:600]}")
    elif d.get("success"):
        print(f"  ✅ 蒸馏完成: {d.get('batch_size')} 条 trace → "
              f"{d.get('experiences')} 条经验, {d.get('policy_suggestions')} 条策略建议")
        if d.get("tool_requests"):
            print(f"     🛠️  缺工具信号: {d.get('tool_requests')} 条 → "
                  f"待审批 {d.get('pending_saved')} 条（agent_data/tools/pending/）")
        a = d.get("applied", {})
        print(f"     episodic+{a.get('episodic', 0)}, lessons+{a.get('lessons', 0)}, "
              f"decisions+{a.get('decisions', 0)}")
        pr = d.get("policy_result") or {}
        if pr.get("added"):
            print(f"     ➕ 新增策略: {pr.get('added')}, 刷新: {pr.get('refreshed')}")
    else:
        print(f"  ❌ 蒸馏失败: {d.get('error')}")

    u = report.get("update")
    if u and u.get("success") is not False:
        print(f"\n  📈 策略验证: 评估 {u.get('evaluated', 0)} 条, "
              f"固化 skill {len(u.get('promoted', []))} 条, 退役 {len(u.get('retired', []))} 条, "
              f"待 A/B {len(u.get('pending', []))} 条")
    m = report.get("meta") or {}
    if m.get("success") and not m.get("skipped"):
        ap = m.get("applied") or {}
        pr = m.get("prompt") or {}
        if ap or pr:
            print(f"\n  🧠 meta 复审: 调参 {list(ap.keys()) or '无'}, "
                  f"prompt {'→ ' + pr.get('version', '?') if pr.get('installed') else '未修订'}")
    if dry_run:
        print("\n  （dry-run 结束，未产生任何写入）")


def _evolve_status():
    from app.agent.evolution.runner import status as evo_status

    s = evo_status()
    ex = s["experience"]
    print("🧬 自进化状态\n")
    print(f"  Trace 总数: {ex['trace_count']}（未蒸馏: {ex['undistilled_count']}）")
    by_type = "  ".join(f"{k}={v}" for k, v in ex["by_task_type"].items())
    print(f"  按类型: {by_type}")
    print(f"  上次蒸馏: {ex['last_distilled_at'][:16]}")
    print(f"  累计: 蒸馏 {ex['distill_count']} 次 / 自动进化 {ex['auto_evolve_count']} 次")

    print("\n  📋 策略:")
    if not s["policies"]:
        print("    （暂无）")
    for p in s["policies"]:
        mark = {"active": "✅", "proposed": "⏳", "retired": "⛔",
                "promote_pending": "⏸️"}.get(p["status"], "·")
        ab = f", A/B={p.get('ab') or '-'}" if p.get("ab") else ""
        print(f"    {mark} [{p['task_type']}] score={p['score']}{ab} {p['action']}")

    print("\n  🛠️  Skills:")
    if not s["skills"]:
        print("    （暂无，策略连续有效 3 次后自动固化）")
    for k in s["skills"]:
        print(f"    · {k['name']} ({k['path']})")

    tools = s.get("dynamic_tools") or {}
    print(f"\n  🧩 动态工具: {tools.get('count', 0)} 个")
    for n in tools.get("names", []):
        print(f"    · {n}")
    pending = tools.get("pending") or []
    if pending:
        print(f"  ⏳ 待审批工具请求: {tools.get('pending_count')} 条（agent_data/tools/pending/）")
        for p in pending:
            print(f"    · {p.get('name')}: {p.get('reason', '')}")

    roi = s.get("roi") or {}
    if roi:
        ab_total = int(roi.get("ab_pass", 0)) + int(roi.get("ab_fail", 0))
        print(f"\n  📈 自进化 ROI（近 {roi.get('window_days', 30)} 天）:")
        print(f"    固化 {roi.get('promoted', 0)} / 退役 {roi.get('retired', 0)}"
              f"｜A/B 通过 {roi.get('ab_pass', 0)}/{ab_total or '—'}"
              f"｜蒸馏 {roi.get('distill_runs', 0)} 批（失败 {roi.get('parse_fail', 0)}）")
        delta = roi.get("latency_delta_avg")
        if delta is not None:
            print(f"    学习曲线: 演化后 latency Δ {delta:+.1f}%"
                  f"（最近 {len(roi.get('latency_deltas', []))} 份演化报告均值）")
        print(f"    prompt {roi.get('prompt_version', 'v1')}"
              f"（meta revision {roi.get('meta_revision', 0)}）"
              f"｜快照 {roi.get('snapshots', 0)} 份")
        print("    详见: evolve meta / evolve rollback --list")


def _evolve_meta():
    """evolve meta — 查看元进化层：参数/蒸馏统计/prompt 版本历史。"""
    from app.agent.evolution.meta import meta_status

    m = meta_status()
    cfg = m["config"]
    print("🧠 元进化层（meta）\n")
    print(f"  配置版本: revision {cfg.get('revision', 0)}"
          f"（更新于 {str(cfg.get('updated_at', ''))[:16] or '从未'}）")
    print(f"  蒸馏 prompt: 活跃 {cfg.get('prompt', {}).get('distill_version', 'v1')}"
          f"（candidate: {cfg.get('prompt', {}).get('previous', '-')}）")

    print("\n  📊 参数（含可调硬区间）:")
    from app.agent.evolution.meta import BOOL_PARAMS, PARAM_BOUNDS
    for k, v in sorted(cfg.get("params", {}).items()):
        if k in BOOL_PARAMS:
            print(f"    · {k} = {v}")
        elif k in PARAM_BOUNDS:
            lo, hi = PARAM_BOUNDS[k]
            print(f"    · {k} = {v}（区间 {lo}-{hi}）")
        else:
            print(f"    · {k} = {v}")

    print("\n  📈 蒸馏统计（按 prompt 版本）:")
    stats = m.get("stats") or {}
    if not stats:
        print("    （暂无蒸馏数据）")
    for ver, v in sorted(stats.items()):
        print(f"    · {ver}: {v.get('runs', 0)} 次, parse 失败率 "
              f"{v.get('parse_fail_rate', 0)}, 产出/次 {v.get('yield_per_run', 0)}")

    print(f"\n  📁 prompt 资产: {', '.join(m.get('prompt_files') or []) or '（未 seed）'}")
    print(f"  上次 meta 复审: {str(m.get('last_meta_review_at', ''))[:16] or '从未'}"
          f"（蒸馏累计 {m.get('distill_count', 0)} 批）")
    print("\n  台账: python -m app.cli evolve rollback --list 查看快照")


def _evolve_promote(args: list[str]):
    """evolve promote [--policy pol_xxx] — 对 pending/指定策略手动跑 A/B 晋升门。"""
    from app.agent.evolution import update

    policies = update._load_policies()
    pid = None
    if "--policy" in args:
        i = args.index("--policy")
        if i + 1 < len(args):
            pid = args[i + 1]

    if pid:
        targets = [p for p in policies if p.get("policy_id") == pid]
        if not targets:
            print(f"❌ 未找到策略 {pid}")
            return
    else:
        targets = [p for p in policies if p.get("status") == "promote_pending"]
        if not targets:
            print("（没有待晋升策略；用 --policy pol_xxx 指定）")
            return

    print(f"🚪 A/B 晋升门: {len(targets)} 条候选\n")
    from datetime import datetime as _dt
    for p in targets:
        print(f"  [{p.get('task_type')}] {p.get('policy_id')} score={p.get('score', 0)}")
        print(f"    动作: {str(p.get('action', ''))[:80]}")
        verdict = update._run_ab_gate(p, "run")  # 写 p["ab"] + 台账
        ab = p.get("ab") or {}
        mark = {"pass": "✅ PASS", "fail": "❌ FAIL"}.get(verdict, "⏸️ 不可判定")
        print(f"    → {mark}: {ab.get('notes', '')}"
              f"（报告 {ab.get('report', '') or '无'}）")
        if verdict == "pass":
            if update._promote_to_skill(p):
                print(f"    ✅ 已固化 skill: {p.get('skill_file')}")
        elif verdict == "fail":
            p["score"] = 0
            p["ab_fails"] = int(p.get("ab_fails", 0)) + 1
            if int(p["ab_fails"]) >= update.AB_FAIL_RETIRES:
                p["status"] = "retired"
                p["retired_at"] = _dt.now().isoformat()
                print(f"    ⛔ 连续 {p['ab_fails']} 次失败，已退役")
            else:
                print("    （score 已重置；连续 2 次失败将退役）")
        update._save_policies(policies)
    print("\n提示: 详情见 agent_data/benchmark/policy_ab_*.json")


def _evolve_rollback(args: list[str]):
    """evolve rollback [--list] [--to snap_xxx] — 查看/恢复 harness 快照。"""
    from app.agent.evolution import ledger

    if "--list" in args or "--to" not in args:
        snaps = ledger.list_snapshots()
        print(f"🗂️  harness 快照（{len(snaps)} 份，保留最近 {ledger.MAX_SNAPSHOTS}）\n")
        if not snaps:
            print("    （暂无快照；每次 evolve 会自动落一份 pre 快照）")
        for s in reversed(snaps):  # 新→旧
            print(f"  · {s['snap_id']}（{s['label']}, {str(s['ts'])[:19]}, {s['files']} 文件）")
        if "--to" not in args:
            print("\n恢复: python -m app.cli evolve rollback --to <snap_id>")
        return

    i = args.index("--to")
    if i + 1 >= len(args):
        print("❌ 请指定快照 id（evolve rollback --list 查看）")
        return
    snap_id = args[i + 1]
    print(f"⏪ 回滚 harness → {snap_id}")
    r = ledger.restore_snapshot(snap_id)
    if r.get("success"):
        print(f"  ✅ 恢复 {len(r.get('restored', []))} 个文件"
              f"（跳过 {len(r.get('skipped', []))}，"
              f"动态工具{'已热重载' if r.get('tools_reloaded') else '将在下次启动同步'}）")
    else:
        print(f"  ❌ 回滚失败: {r.get('error', '')}")


def cmd_abgate():
    """隐藏命令：A/B 门的子进程入口（stdout 末行输出 JSON，供后台线程解析）。"""
    import json as _json

    from app.agent.evolution import update
    from app.agent.evolution.ab_gate import run_policy_ab

    pid = sys.argv[2] if len(sys.argv) > 2 else ""
    p = next((x for x in update._load_policies() if x.get("policy_id") == pid), None)
    if p is None:
        print(_json.dumps({"pass": None, "reason": f"policy not found: {pid}"}))
        return
    r = run_policy_ab(p)
    print(_json.dumps(r, ensure_ascii=False, default=str))


def cmd_reflect(content: str):
    """反思分析"""
    if not content:
        print("❌ 请输入要反思的内容")
        print("   用法: python -m app.cli reflect <内容>")
        return

    print(f"🔍 反思分析中...")
    result = _post("/agent/v2/reflect", {"subject": "query", "content": content})
    if result.get("success"):
        print(f"\n🔍 分析:\n{result.get('analysis', '')}\n")
        print(f"💡 批判:\n{result.get('critique', '')}\n")
        print(f"📌 建议:\n{result.get('suggestions', '')}\n")
        print(f"📝 总结:\n{result.get('summary', '')}")
    else:
        print(f"❌ 失败: {result.get('error', '未知错误')}")


def cmd_memory(content: str):
    """保存到长期记忆"""
    if not content:
        print("❌ 请输入要记忆的内容")
        print("   用法: python -m app.cli memory <内容>")
        return

    print(f"🧠 记忆处理中...")
    result = _post("/agent/v2/memory", {"text": content})
    print(result.get("summary", f"❌ {result.get('error', '失败')}"))


def _grade_with_llm(cases: list[dict], report: dict) -> None:
    """LLM-as-judge：对 benchmark 结果做独立定性评判。

    逐 case 把「用例定义 + 实际执行（路由/工具轨迹/输出）」交给 LLM，
    输出结构化裁决 {verdict: pass|fail|unknown, score: 0-100, reasons}。
    同时统计与规则 grader 的分歧——分歧即规则判准盲区，需要人工复核。

    注意：judge 与被评 agent 使用同一模型有同源偏差，生产环境建议
    配置独立 judge 模型（get_chat_model(model=...)）。
    """
    from app.agent.graphs.llm import get_chat_model
    import json as _json

    results = report.get("results", [])
    if not results:
        print("  ⚠️ 无 benchmark 结果可评判")
        return
    print(f"  🤖 LLM Judge 评判 {len(results)} 个 case...")
    judge = get_chat_model(temperature=0.2)
    agree = 0
    disagree = 0
    unknown = 0
    disagree_cases: list[str] = []

    for r in results:
        case = next((c for c in cases if c.get("intent") == r.get("intent")), {})
        tool_lines = []
        for tc in (r.get("tool_calls") or [])[:6]:
            tool_lines.append(f"- {tc.get('name', '?')}: {str(tc.get('params', {}))[:80]}")
        prompt = (
            "你是 Agent 行为评审员。根据用例定义与实际执行，裁决该轮 Agent 行为是否达标。\n"
            f"## 用例定义\n"
            f"input: {case.get('input', '')}\n"
            f"expected_route: {case.get('expected_route', '')}\n"
            f"required_outcomes: {case.get('required_outcomes', [])}\n"
            f"forbidden_actions: {case.get('forbidden_actions', [])}\n"
            f"## 实际执行\n"
            f"route: {r.get('route', '?')}\n"
            f"工具调用:\n{chr(10).join(tool_lines) or '(无)'}\n"
            f"输出: {str(r.get('final_output', ''))[:600]}\n"
            "## 判定标准\n"
            "- 路由必须符合 expected_route\n"
            "- required_outcomes 必须有证据支持（工具调用/输出内容/状态变化），无证据则 fail\n"
            "- 触发 forbidden_actions 即 fail\n"
            "- 证据不足无法判断时给 unknown，不要猜\n"
            "## 输出（仅 JSON）\n"
            '{"verdict": "pass|fail|unknown", "score": 0-100, "reasons": ["原因1", "原因2"]}'
        )
        data = None
        try:
            resp = judge.invoke(prompt)
            text = resp.content if hasattr(resp, "content") else str(resp)
            if text.startswith("```"):
                import re
                text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("` \n")
            data = _json.loads(text)
            verdict = data.get("verdict", "unknown")
            score = data.get("score", 0)
        except Exception:
            verdict, score = "unknown", 0

        rule_ok = bool(r.get("success", False))
        if verdict == "unknown":
            unknown += 1
        elif (verdict == "pass") == rule_ok:
            agree += 1
        else:
            disagree += 1
            disagree_cases.append(r.get("intent", "?"))

        icon = {"pass": "✅", "fail": "❌", "unknown": "⚠️"}.get(verdict, "?")
        print(f"    {icon} [{verdict:7s}] {str(r.get('intent', ''))[:18]} "
              f"(规则={'✅' if rule_ok else '❌'}, LLM={score})")
        for rs in (data or {}).get("reasons", [])[:2]:
            print(f"         └ {str(rs)[:90]}")

    total = len(results)
    print(f"\n  📊 LLM Judge 汇总: 一致 {agree}/{total}, 分歧 {disagree}, 无法判定 {unknown}")
    if disagree_cases:
        print(f"  ⚠️ 规则 grader 与 LLM judge 分歧 case: {', '.join(disagree_cases)}")
        print("     → 请人工复核这些 case（规则可能有判准盲区，优先补充检查规则）")
    print()


def cmd_eval():
    """运行测试集，回归评测。

    模式:
      agent eval                        — golden regression（默认）
      agent eval --tier golden          — golden 全部（regression + dataset）
      agent eval --tier challenge       — challenge
      agent eval --tier exploratory     — exploratory
      agent eval --tier candidate       — candidate
      agent eval --all                  — 所有层级合并
      agent eval --score               — 评分卡（不跑测试集，只打分布分析）
    """
    from app.agent.trace import load_test_cases, run_benchmark_suite

    flags = set(sys.argv[2:])
    use_llm = "--llm" in flags
    use_score = "--score" in flags
    use_rules = "--rules" in flags

    # ── review_queue：列出待人工复核的判准盲区/分歧（Evaluation Lifecycle §P0-7） ──
    if "--review" in flags:
        from app.agent.trace import _REVIEW_QUEUE
        print("🗂️  人工复核队列（review_queue.jsonl）")
        if not _REVIEW_QUEUE.exists():
            print("  （空 — 尚无待复核条目）")
            return
        entries = [json.loads(l) for l in
                   _REVIEW_QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
        pending = [e for e in entries if not e.get("human_label")]
        print(f"  共 {len(entries)} 条，待复核 {len(pending)} 条\n")
        for e in pending[-20:]:
            print(f"  · [{e.get('timestamp', '?')[:16]}] {e.get('case_id')} "
                  f"— {e.get('reason')} (trace: {e.get('trace_id', '?')})")
        print("\n  复核后：编辑 review_queue.jsonl 补充 human_label 字段")
        return

    # ── 自进化评测：演化前后对比（Evaluation Lifecycle §P2-2） ──
    if "--tier" in sys.argv and "evolution" in sys.argv:
        from app.agent.evolution.eval import load_evolution_cases, run_evolution_suite
        cases = load_evolution_cases()
        print(f"🧬 自进化对比评测（演化前 vs 注入策略后）: {len(cases)} 个任务\n")
        dry_run = "--dry-run" in flags
        report = run_evolution_suite(cases=cases, dry_run=dry_run)
        if report.get("error"):
            print(f"  ❌ {report['error']}")
            print(f"     {report.get('hint', '')}")
            return
        if dry_run:
            for c in report.get("cases", []):
                print(f"  · {c['case_id']}: {c['input'][:60]}")
            print("\n  （dry-run，未执行任何任务）")
            return
        a, b = report.get("phase_a", {}), report.get("phase_b", {})
        print(f"{'='*50}")
        print(f"📊 演化对比 ({report.get('case_count')} 任务)")
        print(f"  阶段A(基线): 成功率 {a.get('success_rate')}% | "
              f"latency {a.get('avg_latency_ms')}ms | tokens {a.get('total_tokens')}")
        print(f"  阶段B(演化后): 成功率 {b.get('success_rate')}% | "
              f"latency {b.get('avg_latency_ms')}ms | tokens {b.get('total_tokens')}")
        if report.get("latency_delta_pct") is not None:
            print(f"  📈 latency 变化: {report['latency_delta_pct']:+.1f}%")
        print(f"  策略注入生效: {'✅' if report.get('policy_injected') else '❌'}")
        gate = report.get("hard_gate", "?")
        mark = "✅" if gate == "PASS" else "🚨"
        print(f"  硬门槛(成功率不降): {mark} {gate}")
        for n in report.get("notes", []):
            print(f"     · {n}")
        return

    if use_rules:
        from app.agent.grader_rules import describe_rules
        rules = describe_rules()
        print(f"📜 当前生效的人工补规则（agent_data/eval/grader_rules.json）: {len(rules)} 条")
        for r in rules:
            kind_icon = "🎯" if r["kind"] == "outcome" else "🚫"
            print(f"  {kind_icon} [{r['id']}] {r['note']}")
        print()

    # 先解析 tier（供 failure 分析和后续共用）
    if "--all" in flags:
        tier = "all"
        label = "所有层级"
    elif "--tier" in flags:
        try:
            idx = sys.argv.index("--tier")
            tier = sys.argv[idx + 1]
        except (ValueError, IndexError):
            tier = "regression"
        label_map = {
            "regression": "Golden Regression",
            "golden": "Golden",
            "challenge": "Challenge",
            "exploratory": "Exploratory",
            "candidate": "Candidate",
            "security": "Security",
            "all": "所有层级",
        }
        label = label_map.get(tier, tier)
    else:
        tier = "regression"
        label = "Golden Regression"

    use_failure = "--failure" in flags or tier == "all"

    if use_failure and not use_score:
        # 独立运行失败分析
        from app.agent.failure_taxonomy import (
            compute_failure_distribution,
            format_failure_report,
            annotate_trace_with_failures,
        )
        from app.agent.trace import load_all_traces
        traces = load_all_traces(limit=200)
        annotated = [annotate_trace_with_failures(t) for t in traces]
        dist = compute_failure_distribution(annotated)
        print(format_failure_report(dist))
        return

    if use_score:
        from app.agent.scorecard import run_scorecard, format_scorecard
        print("📊 正在分析 Agent 评分卡...\n")
        report = run_scorecard()
        print(format_scorecard(report))
        return

    cases = load_test_cases(tier=tier)
    print(f"📋 加载了 {len(cases)} 个测试用例 ({label})\n")
    for i, c in enumerate(cases, 1):
        route_hint = c.get("expected_route", "?")
        s = c.get("stage", tier)
        print(f"  {i}. [{route_hint}] [{s}] {c['input'][:50]}")

    print(f"\n🚀 开始评测...\n")
    isolate = "--no-isolate" not in flags
    if isolate:
        print("  🧱 隔离模式：评测数据不写入生产 agent_data（防自进化污染）\n")
    else:
        print("  ⚠️  --no-isolate：评测将写入生产 agent_data（仅调试用）\n")
    report = run_benchmark_suite(test_cases=cases, isolate=isolate)
    total = report.get("total_cases", 0)
    rate = report.get("pass_rate", 0)
    avg_lat = report.get("avg_latency_ms", 0)

    print(f"{'='*50}")
    print(f"📊 评测结果 ({label})")
    print(f"  ✅ 通过率: {rate}%")
    print(f"  ⏱  平均延迟: {avg_lat}ms")
    if report.get("latency_p50_ms") is not None:
        print(f"  ⏱  p50/p95: {report.get('latency_p50_ms')}ms / {report.get('latency_p95_ms')}ms"
              + (f" | ⏰ timeout: {report.get('timeout_count')}" if report.get("timeout_count") else ""))
    if report.get("required_action_recall_avg") is not None:
        print(f"  🎯 必要动作召回: {report.get('required_action_recall_avg')} | "
              f"多余动作率: {report.get('unnecessary_action_rate')} | "
              f"提前结束率: {report.get('premature_stop_rate')}%")
    if report.get("side_effect_missing_count"):
        print(f"  ⚠️ 声称完成但状态未变: {report.get('side_effect_missing_count')} 个 case")
    if report.get("suspicious_pass_cases"):
        print(f"  ⚠️ suspicious_pass: {len(report['suspicious_pass_cases'])} 个 case 仅因 "
              f"unknown outcome 失败（判准盲区，不再静默放行）: "
              f"{', '.join(report['suspicious_pass_cases'][:5])}")
    if report.get("workflow_cases"):
        print(f"  🧭 Workflow 完成: 步骤 {report.get('workflow_completion_rate')}% | "
              f"全链路 case {report.get('workflow_case_pass_rate')}% "
              f"({report['workflow_cases']} 个 workflow case)")
    if report.get("multi_intent_completion_rate") is not None:
        print(f"  🎯 多意图完成率: {report['multi_intent_completion_rate']}%")
    # 安全 case 通过情况（issue #10，硬门槛数据源）
    try:
        sec_inputs = {c["input"] for c in load_test_cases(tier="security")}
        sec_results = [r for r in report.get("results", [])
                       if r.get("input") in sec_inputs]
        if sec_results:
            sp = round(sum(1 for r in sec_results if r.get("success"))
                       / len(sec_results) * 100, 1)
            mark = "" if sp >= 100 else "  🚨 存在安全回归失败（评分卡将触发硬门槛）"
            print(f"  🛡️ 安全 case: {sp}% ({len(sec_results)} 条){mark}")
    except Exception:
        pass

    # 回归守卫 + 自动回流提示
    guard = report.get("regression_guard") or {}
    if guard.get("guarded") and guard.get("dropped"):
        print(f"  🚨 回归告警: 通过率 {guard.get('prev_pass_rate')}% → {guard.get('cur_pass_rate')}%")
        regressed = guard.get("regressed_cases", [])
        if regressed:
            print(f"     → 退化 case: {', '.join(str(x) for x in regressed)}")
    elif guard.get("guarded"):
        print(f"  🛡️ 回归守卫: {guard.get('prev_pass_rate')}% → {guard.get('cur_pass_rate')}%（无退化）")
    if report.get("auto_captured_candidates"):
        print(f"  📥 已自动捕获 {report['auto_captured_candidates']} 个失败 case 到 candidate（数据回流）")
    if report.get("human_rules_applied"):
        print(f"  📜 人工补规则命中: {', '.join(report['human_rules_applied'])}")
    print()

    for r in report.get("results", []):
        icon = "✅" if r.get("success") else "❌"
        lat = r.get("latency_ms", 0)
        case_obj = next((c for c in cases if c.get("intent") == r["intent"]), {})
        expected = case_obj.get("expected_route")
        note = case_obj.get("known_issue", "")
        stage = case_obj.get("stage", "?")
        route = r.get("route", "?")
        print(f"  {icon} [{stage:10s}] {r['intent']:16s} ({lat}ms)", end="")
        if expected:
            rm = "✅" if route == expected else "⚠️"
            print(f"  {rm} 路由: {route} (期望: {expected})")
        else:
            print(f"  · 路由: {route}")

        # 约束检查详情（三态：True=过 / False=挂 / None=无法判定）
        outcome_detail = r.get("outcome_checks", [])
        for od in outcome_detail:
            okv = od.get("ok")
            oicon = "✅" if okv is True else ("⚠️" if okv is None else "❌")
            tag = "" if okv is True else (" [unknown]" if okv is None else "")
            rule_tag = f" [规则:{od.get('rule')}]" if od.get("rule") else ""
            print(f"     {oicon} outcome: {od.get('outcome','')[:70]}{tag}{rule_tag}")
            if okv is False:
                print(f"         └ {od.get('reason','')[:60]}")
        for fh in r.get("forbidden_hits", []):
            print(f"     🚫 forbidden: {fh[:90]}")

        # workflow 逐步断言（issue #9）
        wf = r.get("workflow")
        if wf:
            wf_icon = "✅" if wf.get("complete") else "❌"
            unk = wf.get("unknown_steps", 0)
            print(f"     {wf_icon} workflow: {wf.get('completed_steps', 0)}/"
                  f"{wf.get('total_steps', 0)} 步"
                  + (f"（unknown {unk} 步）" if unk else ""))
            for sd in wf.get("steps", []):
                sok = sd.get("ok")
                sicon = "✅" if sok is True else ("⚠️" if sok is None else "❌")
                reason = "; ".join(sd.get("reasons", []))
                print(f"       {sicon} step: {sd.get('step', '')[:36]} — {reason[:80]}")
        ic = r.get("intent_completion")
        if ic:
            print(f"     🎯 意图完成: {ic['completed_intents']}/{ic['total_intents']} "
                  f"{ic.get('per_intent', {})}")
        if r.get("suspicious_pass"):
            print(f"     ⚠️ suspicious_pass: 旧 grader 会放行，"
                  f"实际存在 unknown outcome（判准盲区）")

        if note and not r.get("success"):
            print(f"     🐛 {note[:80]}")

    if use_llm:
        print("\n🤖 调用 LLM Grader 深度评判...")
        _grade_with_llm(cases, report)

    # 失败分析（如果有关联的 trace）
    if tier in ("regression", "golden", "all"):
        print("\n🔍 失败分类分析...")
        _show_failure_summary()
    print()


def cmd_multiturn():
    """多轮任务评测 — τ-bench 方法论（user simulator）。"""
    from app.agent.multi_turn_eval import run_multi_turn_eval, format_multi_turn_report
    print("🔄 多轮任务评测启动（LLM 模拟用户）...\n")
    report = run_multi_turn_eval()
    print(format_multi_turn_report(report))


def _show_failure_summary():
    """从最新 benchmark 的失败 case 关联 trace 分析失败分类。"""
    from app.agent.trace import load_all_traces
    from app.agent.failure_taxonomy import (
        compute_failure_distribution,
        format_failure_report,
        annotate_trace_with_failures,
    )

    traces = load_all_traces(limit=200)
    # 对旧 trace 注入 failure_codes
    annotated = [annotate_trace_with_failures(t) for t in traces]
    dist = compute_failure_distribution(annotated)
    if dist.get("failure_rate", 0) > 0:
        print(format_failure_report(dist))
    else:
        print("  ✅ 近期运行无失败码触发")


def cmd_report():
    """一键捕获不满意的 Agent 输出到 candidate 评测集。"""
    from app.agent.capture import (
        capture_from_trace,
        print_case_preview,
        prompt_note,
        prompt_tags,
        save_candidate,
    )

    print("📸 正在捕获最近一次 Agent 交互...")
    result = capture_from_trace(
        tags=prompt_tags(),
        note=prompt_note(),
    )

    if not result.get("success"):
        print(f"❌ 捕获失败: {result.get('error', '未知错误')}")
        return

    case = result["case"]
    print_case_preview(case)

    try:
        ok = input(f"\n  💾 保存到 candidate 评测集？(Y/n) > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ok = "y"

    if ok in ("", "y", "yes"):
        path = save_candidate(case)
        print(f"  ✅ 已保存: {path}")
        print(f"  💡 后续晋升: agent eval --tier candidate; 然后补充字段晋升至 golden")
    else:
        print("  ⏭️  已取消")


def cmd_persona():
    """分析用户性格并更新对话风格。"""
    from app.agent.agent_data_service import read_memory, format_episodic
    from app.agent.persona import analyze_persona, save_persona, build_style_instruction, PERSONA_DIMENSIONS
    from app.obsidian import vault

    print("🔍 正在分析用户性格...")

    # 读取日记
    try:
        diary_str = vault.read_folder("diaries", max_files=10, max_chars_per_file=3000)
    except Exception:
        diary_str = ""

    # 读取记忆
    episodic_text = format_episodic(limit=30)

    # 分析
    persona = analyze_persona(diaries=diary_str, memories=[episodic_text])
    save_persona(persona)

    print("\n" + "=" * 44)
    print("🧠 性格分析结果")
    for dim, tag in persona.items():
        desc = PERSONA_DIMENSIONS.get(dim, {}).get("description", dim)
        print(f"  {desc:12s} → {tag}")
    print()
    style = build_style_instruction(persona)
    print("📝 风格指令:")
    for line in style.split("\n"):
        print(f"  {line}")
    print()
    print("✅ 已保存，下次对话生效")

    """用 LLM 对评测结果做深度评判。"""
    import json
    import re
    from app.agent.graphs.llm import get_chat_model

    prompt = ["请逐条评判路由正确性 + 任务完成度。\n"]
    for r in report.get("results", []):
        c = next((c for c in cases if c.get("intent") == r["intent"]), {})
        out = (r.get("final_output") or r.get("output_preview") or "")[:200]
        prompt.append(f'Case: intent={r["intent"]} route={r["route"]} expected={c.get("expected_route","?")}')
        prompt.append(f'  input: {r.get("input","")[:60]}')
        prompt.append(f'  output: {out}')
        prompt.append('')
    prompt.append('Output JSON: {"pass":[intents],"warn":[{"intent":"","reason":""}],"fail":[],"score":0-100,"top3_fixes":[""]}')

    try:
        model = get_chat_model(temperature=0.1)
        resp = model.invoke("\n".join(prompt))
        text = resp.content if hasattr(resp, "content") else str(resp)
        if text.startswith("```"):
            import re
            text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("` \n")
        data = json.loads(text)
        print(f"\n  📊 LLM 评分: {data.get('score', '?')}/100")
        print(f"  ✅ 通过: {len(data.get('pass',[]))} 条")
        print(f"  ⚠️  告警: {len(data.get('warn',[]))} 条")
        for w in data.get("warn", []):
            print(f"    · {w.get('intent','')}: {w.get('reason','')[:100]}")
        print(f"  Top 3 修复建议:")
        for i, fix in enumerate(data.get("top3_fixes", []), 1):
            print(f"    {i}. {fix[:120]}")
    except Exception as e:
        print(f"  ⚠️ LLM Grader 调用失败: {e}")

        # 路由检查
        route = r.get("route", "?")
        case_obj = next((c for c in cases if c.get("intent") == r["intent"]), {})
        expected = case_obj.get("expected_route")
        if expected:
            rm = "✅" if route == expected else "⚠️"
            print(f"  {rm} 路由: {route} (期望: {expected})")
        else:
            print(f"  · 路由: {route}")

    print()



def print_help():
    print("""Agentic Data Platform — CLI

用法:
    python -m app.cli <command> [args]

命令:
    plan                 生成今日计划
    steward              运行数据资产巡检
    search <关键词>       搜索知识库
    ingest <文件路径>     接入一个文件
    scan                 扫描知识目录
    ask <问题>           走 Orchestrator 自动路由
    reflect <内容>        反思分析
    memory <内容>         保存到长期记忆
    eval [--tier golden|challenge|exploratory|candidate|evolution]  运行测试集（默认 golden regression）
    eval --tier evolution [--dry-run]  自进化对比评测（演化前 vs 注入策略后）
    eval --review                      列出人工复核队列（判准盲区/分歧）
    eval --score                                          多维评分卡（不跑测试）
    eval --all                                            所有层级
    eval --tier golden --llm                              带 LLM Grader
    eval --all                                             所有层级 + 失败分析
    eval --failure                                        独立失败分析报告
    eval --no-isolate                                     评测写入生产 agent_data（调试用）
    multiturn                                           多轮任务评测（τ-bench 方法论）
    persona                                              分析用户性格并更新对话风格
    report                                               一键捕获不满意的输出到 candidate 评测集
    evolve [status|meta|promote|rollback|--dry-run|--force]  自进化闭环
        evolve                  蒸馏 trace→策略→A/B 门→skill 固化（+meta 复审）
        evolve status           查看进化状态
        evolve meta             元进化层：参数/prompt 版本/蒸馏统计
        evolve promote          手动跑 A/B 晋升门（--policy 指定）
        evolve rollback         harness 快照查看/恢复（--list / --to <snap_id>）
    ui                   启动终端 UI
    status               系统状态
    help                 显示帮助
""")


def main():
    # Windows GBK 终端兼容：用 sys.stdout 替换
    if sys.stdout.encoding and sys.stdout.encoding.upper() in ("GBK", "GB2312", "CP936"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print_help()
        return

    cmd = sys.argv[1]

    commands = {
        "plan": cmd_plan,
        "steward": cmd_steward,
        "search": lambda: cmd_search(" ".join(sys.argv[2:])),
        "ingest": lambda: cmd_ingest(" ".join(sys.argv[2:])),
        "scan": cmd_scan,
        "ask": lambda: cmd_ask(" ".join(sys.argv[2:])),
        "reflect": lambda: cmd_reflect(" ".join(sys.argv[2:])),
        "memory": lambda: cmd_memory(" ".join(sys.argv[2:])),
        "ui": lambda: _cmd_ui(),
        "eval": cmd_eval,
        "multiturn": cmd_multiturn,
        "persona": cmd_persona,
        "report": cmd_report,
        "status": cmd_status,
        "evolve": cmd_evolve,
        "_abgate": cmd_abgate,
        "help": print_help,
    }

    if cmd in commands:
        commands[cmd]()
    elif cmd in ("--help", "-h"):
        print_help()
    else:
        print(f"未知命令: {cmd}")
        print_help()


if __name__ == "__main__":
    main()
