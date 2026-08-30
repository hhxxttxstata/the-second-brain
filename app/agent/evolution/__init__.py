"""Self-Evolution 包 — 三层自进化（L1 memory / L2 policy / L3 tool 预留）。

流水线:
  trace 采集(已有) → distill(L1 蒸馏) → update(L2 policy/skill) → 注入上下文
"""
from .runner import evolve_now, maybe_auto_evolve, status

__all__ = ["evolve_now", "maybe_auto_evolve", "status"]
