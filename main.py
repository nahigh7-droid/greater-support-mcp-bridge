import os
from typing import Any, Dict, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route


WP_BASE_URL = os.environ.get("WP_BASE_URL", "").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")


def require_env() -> None:
    missing = [
        name
        for name, value in {
            "WP_BASE_URL": WP_BASE_URL,
            "WP_USERNAME": WP_USERNAME,
            "WP_APP_PASSWORD": WP_APP_PASSWORD,
            "MCP_API_KEY": MCP_API_KEY,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


def token_is_valid(request: Request) -> bool:
    auth_header = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    query_key = request.query_params.get("api_key", "")

    token = auth_header.strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    return (
        token == MCP_API_KEY
        or x_api_key == MCP_API_KEY
        or query_key == MCP_API_KEY
    )


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Keep root public so Fly health/root checks do not fail.
        if request.url.path == "/":
            return await call_next(request)

        # Protect direct checks and MCP routes.
        protected_paths = (
            "/sse",
            "/messages",
            "/health",
            "/capabilities",
        )

        if request.url.path.startswith(protected_paths):
            require_env()
            if not token_is_valid(request):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


async def wordpress_request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    require_env()

    url = f"{WP_BASE_URL}{path}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method=method,
            url=url,
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


mcp = FastMCP("Greater Support WordPress Drafts")


@mcp.tool()
async def create_draft_post(
    title: str,
    content: str,
    excerpt: Optional[str] = None,
    slug: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a WordPress draft post for Greater Support after human approval only.

    This tool must never publish, delete, or modify live content.
    """
    if not title or not content:
        raise ValueError("title and content are required.")

    payload: Dict[str, Any] = {
        "title": title,
        "content": content,
    }

    if excerpt:
        payload["excerpt"] = excerpt

    if slug:
        payload["slug"] = slug

    result = await wordpress_request("POST", "/draft", payload)

    return {
        "ok": True,
        "action": "create_draft_post",
        "message": "WordPress draft post created. Review in WordPress before publishing.",
        "wordpress_result": result,
    }


@mcp.tool()
async def update_draft_post(
    post_id: int,
    title: Optional[str] = None,
    content: Optional[str] = None,
    excerpt: Optional[str] = None,
    slug: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update an existing WordPress draft post for Greater Support after human approval only.

    This tool must never publish, delete, or modify live content.
    """
    if not post_id:
        raise ValueError("post_id is required.")

    payload: Dict[str, Any] = {}

    if title:
        payload["title"] = title

    if content:
        payload["content"] = content

    if excerpt:
        payload["excerpt"] = excerpt

    if slug:
        payload["slug"] = slug

    if not payload:
        raise ValueError("At least one update field is required.")

    result = await wordpress_request("PATCH", f"/draft/{post_id}", payload)

    return {
        "ok": True,
        "action": "update_draft_post",
        "message": "WordPress draft post updated. Review in WordPress before publishing.",
        "wordpress_result": result,
    }


async def root(request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": "greater-support-mcp-bridge",
            "mcp_url": "/sse",
            "routes": ["/", "/health", "/capabilities", "/sse"],
            "tools": ["create_draft_post", "update_draft_post"],
        }
    )


async def health(request: Request) -> Response:
    result = await wordpress_request("GET", "/health")
    return JSONResponse(result)


async def capabilities(request: Request) -> Response:
    result = await wordpress_request("GET", "/capabilities")
    return JSONResponse(result)


app = Starlette(
    routes=[
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/capabilities", capabilities, methods=["GET"]),
        Mount("/", app=mcp.sse_app()),
    ],
    middleware=[
        Middleware(ApiKeyMiddleware),
    ],
)
