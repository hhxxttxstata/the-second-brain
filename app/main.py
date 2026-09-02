"""FastAPI application entry point — Obsidian-native Agent."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes_agent_v2 import router as agent_v2_router
from app.api.routes_workspace import router as workspace_router
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
app.include_router(workspace_router)

# Workspace 前端（frontend/dist 由 npm run build 产出）。
# 挂载在 include_router 之后：/workspace/summary 等 API 路由优先于静态文件。
# Windows 注册表可能把 .js 映射成 text/plain，导致模块脚本被浏览器拒载——显式纠正。
import mimetypes

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/wasm", ".wasm")


class _DevStaticFiles(StaticFiles):
    """dist 产物每次协商缓存（etag 304），避免改版后浏览器沿用旧响应头。"""

    def file_response(self, *args: object, **kwargs: object):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/workspace", _DevStaticFiles(directory=_FRONTEND_DIST, html=True), name="workspace")

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/workspace/")

logger.info("obsidian_agent_ready", vault=settings.obsidian_vault)
