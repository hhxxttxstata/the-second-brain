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
    import uuid
    result = run_orchestrator(input_text=question, thread_id=f"ask_{uuid.uuid4().hex[:8]}")

    if result.get("success"):
        route = result.get("route", "?")
        print(f"  路由: {route} ({result.get('route_reason', '')})")
        print(f"  ⏱  {result.get('latency_ms', 0)}ms")
        print()
        if result.get("result"):
            print(result["result"])
        if result.get("result_data"):
            print(f"\n  (data: {result['result_data']})")
    else:
        print(f"❌ 失败: {result.get('error', '未知错误')}")


def _cmd_ui():
    """启动终端 UI"""
    from app.webui import main as tui_main
    tui_main()


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

    if use_score:
        from app.agent.scorecard import run_scorecard, format_scorecard
        print("📊 正在分析 Agent 评分卡...\n")
        report = run_scorecard()
        print(format_scorecard(report))
        return

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
            "all": "所有层级",
        }
        label = label_map.get(tier, tier)
    else:
        tier = "regression"
        label = "Golden Regression"

    cases = load_test_cases(tier=tier)
    print(f"📋 加载了 {len(cases)} 个测试用例 ({label})\n")
    for i, c in enumerate(cases, 1):
        route_hint = c.get("expected_route", "?")
        s = c.get("stage", tier)
        print(f"  {i}. [{route_hint}] [{s}] {c['input'][:50]}")

    print(f"\n🚀 开始评测...\n")
    report = run_benchmark_suite(test_cases=cases)
    total = report.get("total_cases", 0)
    rate = report.get("pass_rate", 0)
    avg_lat = report.get("avg_latency_ms", 0)

    print(f"{'='*50}")
    print(f"📊 评测结果 ({label})")
    print(f"  ✅ 通过率: {rate}%")
    print(f"  ⏱  平均延迟: {avg_lat}ms\n")

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
        if note and route != expected:
            print(f"     🐛 {note[:80]}")

    if use_llm:
        print("\n🤖 调用 LLM Grader 深度评判...")
        _grade_with_llm(cases, report)
    print()


def _grade_with_llm(cases: list[dict], report: dict) -> None:
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
    eval [--tier golden|challenge|exploratory|candidate]  运行测试集（默认 golden regression）
    eval --score                                          多维评分卡（不跑测试）
    eval --all                                            所有层级
    eval --tier golden --llm                              带 LLM Grader
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
        "status": cmd_status,
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
