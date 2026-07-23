"""FastAPI application entry point — Obsidian-native Agent."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_agent_v2 import router as agent_v2_router
from app.core.config import settings
from app.core.logging import configure_logging, logger
from app.agent.self_eval import run_self_eval, print_report
from app.agent.graphs.tools import get_registry, ensure_mcp_started


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("agent_starting", obsidian_vault=settings.obsidian_vault)

    # 启动自检
    try:
        eval_report = run_self_eval()
        logger.info("self_eval_complete", score=eval_report["overall_score"],
                     issues=len(eval_report.get("issues", [])))
        for issue in eval_report.get("issues", []):
            logger.warning("self_eval_issue", detail=issue)
    except Exception as exc:
        logger.warning("self_eval_skipped", error=str(exc))

    # 启动 MCP 服务器
    try:
        mcp_count = ensure_mcp_started()
        if mcp_count > 0:
            logger.info("mcp_started", tools=mcp_count)
        else:
            logger.info("mcp_async_start", note="servers will connect on first use")
    except Exception as exc:
        logger.warning("mcp_start_skipped", error=str(exc))

    yield
    logger.info("agent_shutting_down")


app = FastAPI(
    title="Personal Knowledge Agent",
    description="Obsidian-native Agent powered by LangGraph + DeepSeek",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.2.0", "mode": "obsidian-native"}


app.include_router(agent_v2_router)

logger.info("obsidian_agent_ready", vault=settings.obsidian_vault)
