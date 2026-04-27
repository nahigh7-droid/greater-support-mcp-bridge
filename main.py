"""
FastAPI application for the Greater Support MCP bridge.

This service exposes a minimal Model Conversation Protocol (MCP) interface over
HTTP that allows a ChatGPT-based agent to create or update draft posts in a
WordPress installation via the Greater Support Content Agent Connector plugin.

It is deliberately restrictive: the only supported actions are draft
creation/update, and there is no ability to publish, delete or otherwise
manipulate content. All requests are authenticated using a bearer token and
WordPress basic authentication configured via environment variables.
"""

import os
from typing import Any, AsyncIterator, Dict

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from sse_starlette.sse import EventSourceResponse


# ---------------------------------------------------------------------------
# Configuration
#
# These environment variables must be set in the hosting environment.  See the
# accompanying README.md for details on how to configure them.

WP_BASE_URL: str = os.environ.get("WP_BASE_URL", "")
"""
Base URL pointing at the Greater Support Content Agent Connector plugin.  The
value should include the `/v1` suffix and normally looks like:

    https://greatersupport.com.au/wp-json/greater-support-content/v1

This variable is required.  Without it the service cannot proxy requests to
WordPress.
"""

WP_USERNAME: str = os.environ.get("WP_USERNAME", "")
"""
Username for a dedicated WordPress account with the Editor role.  This user
should have the minimal permissions necessary to create and update draft
content but not to publish or delete posts.
"""

WP_APP_PASSWORD: str = os.environ.get("WP_APP_PASSWORD", "")
"""
Application password associated with the WordPress Editor account.  WordPress
application passwords can be generated in the user profile and should be used
instead of the account's primary password.
"""

MCP_API_KEY: str = os.environ.get("MCP_API_KEY", "")
"""
Secret bearer token used to authenticate inbound requests from the agent.  The
agent must send this key in the Authorization header as `Bearer <token>`.  If
the token does not match this environment value the request will be rejected.
"""


def _require_env(var_name: str, value: str) -> None:
    """Raise an exception if a required environment variable is missing."""
    if not value:
        raise RuntimeError(f"Environment variable {var_name} must be set")


# Validate environment configuration at import time.  This fails fast if
# required settings are missing.
_require_env("WP_BASE_URL", WP_BASE_URL)
_require_env("WP_USERNAME", WP_USERNAME)
_require_env("WP_APP_PASSWORD", WP_APP_PASSWORD)
_require_env("MCP_API_KEY", MCP_API_KEY)


# ---------------------------------------------------------------------------
# Application
app = FastAPI(title="Greater Support MCP Bridge")


async def _wordpress_call(
    path: str,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
) -> Any:
    """Proxy a request to the WordPress Content Agent Connector.

    Args:
        path: Path relative to WP_BASE_URL to call (e.g. "/posts/draft").
        method: HTTP method to use (GET, POST or PUT).
        payload: JSON payload for POST/PUT requests.

    Returns:
        The JSON-decoded response from WordPress.

    Raises:
        HTTPException: If the WordPress call fails or an invalid method is used.
    """
    url = f"{WP_BASE_URL}{path}"
    auth = (WP_USERNAME, WP_APP_PASSWORD)
    async with httpx.AsyncClient() as client:
        try:
            if method == "GET":
                response = await client.get(url, auth=auth)
            elif method == "POST":
                response = await client.post(url, json=payload, auth=auth)
            elif method == "PUT":
                response = await client.put(url, json=payload, auth=auth)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Error from WordPress: {exc}") from exc
    return response.json()


@app.get("/health")
async def health() -> Any:
    """Report health of the WordPress Content Agent Connector."""
    return await _wordpress_call("/health")


@app.get("/capabilities")
async def capabilities() -> Any:
    """List capabilities exposed by the WordPress Content Agent Connector."""
    return await _wordpress_call("/capabilities")


@app.post("/sse")
async def sse(
    request: Request,
    authorization: str = Header(default=""),
) -> EventSourceResponse:
    """Handle MCP requests and stream responses via Server‑Sent Events.

    The agent sends a JSON payload with `action` and `data` fields.  This
    endpoint validates the bearer token provided in the Authorization header
    and then dispatches the action to WordPress accordingly.  Only a
    restricted set of actions are supported.
    """
    # Validate Authorization header
    token_prefix = "Bearer "
    if not authorization.startswith(token_prefix):
        raise HTTPException(status_code=401, detail="Authorization header missing or malformed")
    token = authorization[len(token_prefix) :]
    if token != MCP_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid MCP API key")

    # Parse request payload
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    action = payload.get("action")
    data: Dict[str, Any] = payload.get("data", {})

    async def event_stream() -> AsyncIterator[Dict[str, Any]]:
        """Generate events for SSE response."""
        try:
            # Route based on action
            if action == "createDraftPost":
                # Call POST /posts/draft to create a new draft blog post
                result = await _wordpress_call("/posts/draft", method="POST", payload=data)
                yield {"event": "result", "data": result}
            elif action == "updateDraftPost":
                # Must provide an ID; update the specified draft post
                post_id = data.get("id")
                if not post_id:
                    yield {"event": "error", "data": {"message": "Missing id for updateDraftPost"}}
                else:
                    result = await _wordpress_call(f"/posts/{post_id}", method="PUT", payload=data)
                    yield {"event": "result", "data": result}
            else:
                yield {
                    "event": "error",
                    "data": {"message": f"Unsupported action: {action}"},
                }
        except Exception as exc:
            # Catch any exception and emit an error event
            yield {
                "event": "error",
                "data": {"message": str(exc)},
            }

    return EventSourceResponse(event_stream())