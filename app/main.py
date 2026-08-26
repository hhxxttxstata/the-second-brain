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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("agent_starting", obsidian_vault=settings.obsidian_vault)

    # 启动自检
    try:
        eval_report = run_self_eval()
        ss = eval_report.get("status_summary", {})
        logger.info("self_eval_complete",
                    status=f"ok={ss.get('ok', 0)} warn={ss.get('warn', 0)} fail={ss.get('fail', 0)}",
                    issues=len(eval_report.get("issues", [])))
        for issue in eval_report.get("issues", []):
            logger.warning("self_eval_issue", detail=issue)
    except Exception as exc:
        logger.warning("self_eval_skipped", error=str(exc))

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
