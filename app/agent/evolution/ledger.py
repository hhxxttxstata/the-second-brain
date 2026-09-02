"""Ledger — 自进化台账 + harness 快照（Governance 层，P4）。

append-only 事件流水：所有自进化动作（蒸馏/策略/A-B 门/工具/meta/回滚）统一打点，
配合快照机制提供"任一时刻还原 harness 状态"的能力
（Darwin Gödel Machine archive 思想的文件级轻量版）。

设计约束：
  - 打点/快照失败绝不影响主流程（异常收敛为日志）
  - 快照与恢复读取各模块的当前路径常量（尊重测试 monkeypatch / 评测隔离重定向）
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

_LEDGER_PATH = settings.agent_data_dir / "evolution" / "ledger.jsonl"
_SNAPSHOTS_DIR = settings.agent_data_dir / "evolution" / "snapshots"

# 快照保留上限（按 snap_id 排序裁剪最旧）
MAX_SNAPSHOTS = 10

# 事件类型清单（自文档化；log_event 不做白名单校验，保持低摩擦）
EVENT_TYPES = [
    "evolve_run", "distill_run",
    "policy_added", "policy_promoted", "policy_retired", "policy_ab_gate",
    "tool_requested", "tool_created", "tool_create_failed",
    "meta_update", "meta_prompt_rollback",
    "snapshot_taken", "rollback",
]


# ---------------------------------------------------------------------------
# 事件流水
# ---------------------------------------------------------------------------

def log_event(event: str, **payload: Any) -> None:
    """追加一条台账事件。失败只记日志，绝不打断调用方。"""
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now().isoformat(), "event": event, **payload}
        with _LEDGER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.error("evolve.ledger.write_failed", event=event, error=str(exc)[:200])


def read_ledger(limit: int = 50, event: str | None = None) -> list[dict[str, Any]]:
    """读台账（返回最近 limit 条，可按事件类型过滤）。文件不存在/损坏返回空。"""
    if not _LEDGER_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in _LEDGER_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    if event:
        rows = [r for r in rows if r.get("event") == event]
    return rows[-limit:]


# ---------------------------------------------------------------------------
# harness 快照
# ---------------------------------------------------------------------------

def _snapshot_targets() -> list[tuple[Path, str]]:
    """(源路径, 快照内相对路径) 列表 —— 动态读取模块常量，只收集存在的文件。"""
    from . import experience as exp, update

    targets: list[tuple[Path, str]] = [
        (exp._STATE_PATH, "evolution/state.json"),
        (update._POLICIES_JSON, "evolution/policies.json"),
        (update._POLICIES_MD, "memory/policies.md"),
    ]

    # meta 层产物（可能尚未初始化，缺文件自动跳过）
    try:
        from . import meta
        targets.append((meta._META_CONFIG, "evolution/meta_config.json"))
        targets.append((meta._META_STATS, "evolution/meta_stats.json"))
    except Exception:
        pass

    # skills（L2 固化产物）
    if update._SKILLS_DIR.is_dir():
        for f in sorted(update._SKILLS_DIR.glob("*.md")):
            targets.append((f, f"memory/skills/{f.name}"))

    # topic memory（L1 经验沉淀）
    try:
        from .. import topic_memory as tm
        for name in ("lessons.md", "decisions.md"):
            targets.append((tm._MEMORY_DIR / name, f"memory/{name}"))
    except Exception:
        pass

    # 动态工具（L3）
    try:
        from app.tool_registry import dynamic_tools as dt
        if dt._TOOLS_DIR.is_dir():
            for f in sorted(dt._TOOLS_DIR.iterdir()):
                if f.suffix in (".py", ".json"):
                    targets.append((f, f"tools/{f.name}"))
    except Exception:
        pass

    return [(src, rel) for src, rel in targets if src.exists()]


def _live_path(rel: str) -> Path | None:
    """快照内相对路径 → 当前真实路径（与 _snapshot_targets 的映射保持一致）。"""
    from . import experience as exp, update

    fixed = {
        "evolution/state.json": exp._STATE_PATH,
        "evolution/policies.json": update._POLICIES_JSON,
        "memory/policies.md": update._POLICIES_MD,
    }
    if rel in fixed:
        return fixed[rel]
    if rel.startswith("memory/skills/"):
        return update._SKILLS_DIR / Path(rel).name
    if rel.startswith("memory/"):
        try:
            from .. import topic_memory as tm
            return tm._MEMORY_DIR / Path(rel).name
        except Exception:
            return None
    if rel.startswith("tools/"):
        try:
            from app.tool_registry import dynamic_tools as dt
            return dt._TOOLS_DIR / Path(rel).name
        except Exception:
            return None
    if rel.startswith("evolution/"):
        try:
            from . import meta
            return {
                "evolution/meta_config.json": meta._META_CONFIG,
                "evolution/meta_stats.json": meta._META_STATS,
            }.get(rel)
        except Exception:
            return None
    return None


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def take_snapshot(label: str = "pre") -> str | None:
    """把当前 harness 可变状态复制为一份带 manifest 的快照。"""
    snap_id = f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}"
    snap_dir = _SNAPSHOTS_DIR / snap_id
    manifest: dict[str, Any] = {
        "snap_id": snap_id, "label": label,
        "ts": datetime.now().isoformat(), "files": {},
    }
    try:
        copied = 0
        for src, rel in _snapshot_targets():
            dst = snap_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            manifest["files"][rel] = _sha256(dst)
            copied += 1
        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        _prune_snapshots()
        log_event("snapshot_taken", snap_id=snap_id, files=copied)
        return snap_id
    except Exception as exc:
        logger.error("evolve.ledger.snapshot_failed", snap_id=snap_id,
                     error=str(exc)[:200])
        return None


def list_snapshots() -> list[dict[str, Any]]:
    """列出可用快照（旧→新），读取各自 manifest。"""
    if not _SNAPSHOTS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(_SNAPSHOTS_DIR.iterdir()):
        mf = d / "manifest.json"
        if not (d.is_dir() and mf.exists()):
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            out.append({"snap_id": m.get("snap_id", d.name),
                        "label": m.get("label", ""),
                        "ts": m.get("ts", ""),
                        "files": len(m.get("files", {}))})
        except (json.JSONDecodeError, OSError):
            continue
    return out


def restore_snapshot(snap_id: str) -> dict[str, Any]:
    """恢复快照：文件回写 + 动态工具热重载 + 图缓存失效。"""
    snap_dir = _SNAPSHOTS_DIR / snap_id
    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return {"success": False, "error": f"快照不存在: {snap_id}"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"success": False, "error": f"manifest 读取失败: {exc}"}

    restored, skipped = [], []
    try:
        for rel in manifest.get("files", {}):
            live = _live_path(rel)
            if live is None:
                skipped.append(rel)
                continue
            src = snap_dir / rel
            if not src.exists():
                skipped.append(rel)
                continue
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, live)
            restored.append(rel)
    except Exception as exc:
        log_event("rollback", snap_id=snap_id, success=False,
                  error=str(exc)[:200], restored=restored)
        return {"success": False, "error": str(exc), "restored": restored}

    tools_reloaded = _reload_dynamic_tools(snap_dir)
    log_event("rollback", snap_id=snap_id, success=True,
              restored=len(restored), skipped=len(skipped))
    return {"success": True, "snap_id": snap_id, "restored": restored,
            "skipped": skipped, "tools_reloaded": tools_reloaded}


def _reload_dynamic_tools(snap_dir: Path) -> bool:
    """按快照重载动态工具注册表：清掉快照中不存在的 dynamic 工具再重新加载。"""
    try:
        from app.agent.graphs.tools import _registry, reset_agent_tools_cache
        from app.tool_registry import dynamic_tools as dt

        snap_tool_names = {p.stem for p in (snap_dir / "tools").glob("*.json")} \
            if (snap_dir / "tools").is_dir() else set()
        # 快照里没有的动态工具 = 回滚目标状态中不存在的 → 移除
        for name in [n for n, t in _registry._native_tools.items()
                     if getattr(t, "source", "") == "dynamic" and n not in snap_tool_names]:
            _registry._native_tools.pop(name, None)
        dt.load_dynamic_tools(_registry)
        reset_agent_tools_cache()
        return True
    except Exception as exc:
        logger.warning("evolve.ledger.tools_reload_skipped", error=str(exc)[:200])
        return False  # 不阻塞回滚：下次启动 load_dynamic_tools 会自动同步


def _prune_snapshots(keep: int = MAX_SNAPSHOTS) -> None:
    if not _SNAPSHOTS_DIR.is_dir():
        return
    dirs = sorted(d for d in _SNAPSHOTS_DIR.iterdir() if d.is_dir())
    for d in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(d, ignore_errors=True)
