"""Structured logging configuration with structlog.

local 模式: 终端 ConsoleRenderer + 文件轮转 (agent_data/logs/app.log)。
文件落盘保证"终端关掉后环节日志仍在", 供事后定位 bug。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from app.core.config import settings


def configure_logging() -> None:
    """Configure structlog as the logging backend."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    handlers: list[logging.Handler] = []

    if settings.app_env == "local":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    # 终端 handler (structlog 直接渲染)
    terminal = logging.StreamHandler()
    handlers.append(terminal)

    # 文件 handler (结构化 JSON 落盘, 轮转 5MB × 3)
    try:
        log_dir = settings.agent_data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(file_handler)
    except Exception:
        pass  # 文件不可写时不阻塞启动

    logging.basicConfig(
        format="%(message)s",
        level=logging.DEBUG if settings.debug else logging.INFO,
        handlers=handlers,
    )

    # structlog: 终端用 ConsoleRenderer, 文件需要 JSON 渲染 → 分别挂 processor
    # 简单方案: 统一用 ConsoleRenderer 渲染后写文件(可读), 终端一致
    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = structlog.get_logger()
