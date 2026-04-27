import asyncio
import json
import os
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse


WP_BASE_URL = os.environ.get("WP_BASE_URL", "").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

app = FastAPI(title="Greater Support MCP Bridge", version="1.0.1")

sessions: Dict[str, asyncio.Queue] = {}


TOOLS = [
    {
        "name": "create_draft_post",
        "description": (
            "Create a WordPress draft post for Greater Support after human approval only. "
            "This tool must never publish, delete, or modify live content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Draft post title.",
                },
                "content": {
                    "type": "string",
                    "description": "Draft post body content in plain text or HTML.",
                },
                "excerpt": {
                    "type": "string",
                    "description": "Optional short summary or excerpt.",
                },
                "slug": {
                    "type": "string",
                    "description": "Optional URL slug.",
                },
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "update_draft_post",
        "description": (
            "Update an existing WordPress draft post for Greater Support after human approval only. "
            "This tool must never publish, delete, or modify live content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "post_id": {
                    "type": "integer",
                    "description": "WordPress draft post ID to update.",
                },
                "title": {
                    "type": "string",
                    "description": "Updated draft post title.",
                },
                "content": {
                    "type": "string",
                    "description": "Updated draft post body content in plain text or HTML.",
                },
                "excerpt": {
                    "type": "string",
                    "description": "Optional updated excerpt.",
                },
                "slug": {
                    "type": "string",
                    "description": "Optional updated URL slug.",
                },
            },
            "required": ["post_id"],
        },
    },
]


def require_env() -> None:
    missing = []
    for key, value in {
        "WP_BASE_URL": WP_BASE_URL,
        "WP_USERNAME": WP_USERNAME,
        "WP_APP_PASSWORD": WP_APP_PASSWORD,
        "MCP_API_KEY": MCP_API_KEY,
    }.items():
        if not value:
            missing.append(key)

    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing required environment variables: {', '.join(missing)}",
        )


def check_auth(request: Request) -> None:
    require_env()

    auth_header = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    query_key = request.query_params.get("api_key", "")

    token = auth_header
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    if token == MCP_API_KEY or x_api_key == MCP_API_KEY or query_key == MCP_API_KEY:
        return

    raise HTTPException(status_code=401, detail="Unauthorized")


async def call_wordpress(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    require_env()

    url = f"{WP_BASE_URL}{path}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method,
            url,
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            json=payload,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"WordPress returned HTTP {response.status_code}: {response.text}"
        )

    try:
        return response.json()
    except Exception:
        return {"ok": True, "raw": response.text}


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "greater-support-mcp-bridge",
        "routes": ["/health", "/capabilities", "/sse", "/messages"],
    }


@app.get("/health")
async def health(request: Request) -> Any:
    check_auth(request)
    return await call_wordpress("GET", "/health")


@app.get("/capabilities")
async def capabilities(request: Request) -> Any:
    check_auth(request)
    return await call_wordpress("GET", "/capabilities")


@app.get("/sse")
async def sse(request: Request) -> EventSourceResponse:
    check_auth(request)

    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    async def event_generator():
        # MCP HTTP/SSE transport tells the client where to POST JSON-RPC messages.
        yield {
            "event": "endpoint",
            "data": f"/messages?session_id={session_id}",
        }

        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    yield message
                except asyncio.TimeoutError:
                    yield {
                        "event": "ping",
                        "data": "{}",
                    }
        finally:
            sessions.pop(session_id, None)

    return EventSourceResponse(event_generator())


@app.post("/messages")
@app.post("/messages/")
async def messages(request: Request) -> JSONResponse:
    session_id = request.query_params.get("session_id")

    if not session_id or session_id not in sessions:
        # If the client posts without a valid session, require direct auth.
        check_auth(request)

    body = await request.json()
    response = await handle_jsonrpc(body)

    # Notifications do not require JSON-RPC responses.
    if response is None:
        return JSONResponse({"ok": True})

    if session_id and session_id in sessions:
        await sessions[session_id].put(
            {
                "event": "message",
                "data": json.dumps(response),
            }
        )
        return JSONResponse({"ok": True})

    return JSONResponse(response)


async def handle_jsonrpc(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # MCP notifications do not need responses.
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {
                        "tools": {
                            "listChanged": False,
                        }
                    },
                    "serverInfo": {
                        "name": "greater-support-wordpress-drafts",
                        "version": "1.0.1",
                    },
                },
            }

        if method == "ping":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {},
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": TOOLS,
                },
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}

            result = await call_tool(tool_name, arguments)

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result),
                        }
                    ],
                    "isError": False,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }

    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": str(exc),
            },
        }


async def call_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if tool_name == "create_draft_post":
        title = arguments.get("title")
        content = arguments.get("content")

        if not title or not content:
            raise ValueError("create_draft_post requires title and content.")

        payload: Dict[str, Any] = {
            "title": title,
            "content": content,
        }

        for optional_key in ["excerpt", "slug"]:
            if arguments.get(optional_key):
                payload[optional_key] = arguments[optional_key]

        wp_result = await call_wordpress("POST", "/draft", payload)

        return {
            "ok": True,
            "action": "create_draft_post",
            "wordpress_result": wp_result,
            "message": "WordPress draft post created. Review in WordPress before publishing.",
        }

    if tool_name == "update_draft_post":
        post_id = arguments.get("post_id")

        if not post_id:
            raise ValueError("update_draft_post requires post_id.")

        payload: Dict[str, Any] = {}

        for optional_key in ["title", "content", "excerpt", "slug"]:
            if arguments.get(optional_key):
                payload[optional_key] = arguments[optional_key]

        if not payload:
            raise ValueError("update_draft_post requires at least one field to update.")

        wp_result = await call_wordpress("PATCH", f"/draft/{post_id}", payload)

        return {
            "ok": True,
            "action": "update_draft_post",
            "wordpress_result": wp_result,
            "message": "WordPress draft post updated. Review in WordPress before publishing.",
        }

    raise ValueError(f"Unknown tool: {tool_name}")
