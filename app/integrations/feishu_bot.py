"""飞书 Bot — 基于纯 Obsidian 架构。"""
from __future__ import annotations

import json
import time
from typing import Any

import requests
from fastapi import APIRouter, Request

from app.agent.graphs.orchestrator import run_orchestrator
from app.agent.graphs.plan_graph import run_plan_graph
from app.core.config import settings
from app.core.logging import logger
from app.integrations.feishu_cards import (error_card, help_card, plan_card,
                                           search_card, status_card,
                                           text_message)
from app.obsidian import vault

router = APIRouter(prefix="/feishu", tags=["飞书"])

_tenant_token: str = ""
_token_expires: float = 0


def _get_tenant_token() -> str:
    global _tenant_token, _token_expires
    if time.time() < _token_expires:
        return _tenant_token
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return ""
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        timeout=10,
    )
    data = resp.json()
    _tenant_token = data.get("tenant_access_token", "")
    _token_expires = time.time() + data.get("expire", 7200) - 60
    return _tenant_token


def _reply_message(open_id: str, content: dict[str, Any]) -> bool:
    token = _get_tenant_token()
    if not token:
        return False
    payload = {
        "receive_id": open_id,
        "msg_type": content["msg_type"],
        "content": json.dumps(content.get("card", content.get("content", ""))),
    }
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "open_id"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    return resp.status_code == 200


def _handle_command(cmd: str, args: str) -> dict:
    cmd = cmd.lower().strip()

    if cmd in ("plan", "计划", "今日计划", "daily"):
        result = run_plan_graph()
        return plan_card(result) if result.get("success") else error_card(result.get("error", ""))

    elif cmd in ("search", "搜索", "查"):
        if not args:
            return text_message("请告诉我搜索关键词")
        kw = args.strip()
        vault_r = vault.search_notes(kw)
        return search_card({"query": kw, "results": [{"text": vault_r}]})

    elif cmd in ("status", "状态", "系统"):
        return status_card({
            "vault": len(list(__import__("pathlib").Path(settings.obsidian_vault).rglob("*.md"))),
        })

    elif cmd in ("help", "帮助", "h"):
        return help_card()

    else:
        full = f"{cmd} {args}".strip()
        result = run_orchestrator(input_text=full)
        if result.get("success"):
            return text_message(result.get("result", ""))
        vault_r = vault.search_notes(full)
        return search_card({"query": full, "results": [{"text": vault_r}]})


@router.post("/webhook")
async def feishu_webhook(req: Request):
    body = await req.json()
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    event = body.get("event") or {}
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return {"code": 0}

    content_raw = message.get("content", "{}")
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except json.JSONDecodeError:
        return {"code": 0}

    text = content.get("text", "").strip()
    bot_name = settings.feishu_bot_name
    for prefix in [f"@{bot_name}", f"@_{bot_name}", "/"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if not text:
        return {"code": 0}

    parts = text.split(None, 1)
    cmd, args = (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")

    try:
        reply = _handle_command(cmd, args)
    except Exception as e:
        logger.error("feishu_cmd_failed", cmd=cmd, error=str(e))
        reply = error_card(str(e)[:200])

    sender = event.get("sender", {})
    open_id = sender.get("sender_id", {}).get("open_id", message.get("chat_id", ""))
    if open_id:
        _reply_message(open_id, reply)
    return {"code": 0}
