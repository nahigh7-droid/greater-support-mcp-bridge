# Greater Support MCP Bridge

This repository contains a simple FastAPI service that implements a minimal
Model Conversation Protocol (MCP) server for Greater Support.  The MCP bridge
provides a `/sse` endpoint that proxies requests from a ChatGPT‐based agent
into the Greater Support WordPress Content Agent Connector plugin.  It
supports only draft creation and updates – there is no ability to publish or
delete posts.

## Features

- **Health check and capabilities** forwarding to the WordPress plugin.
- **Secure**: all requests must include a bearer token (`MCP_API_KEY`) in the
  `Authorization` header.
- **Limited actions**: only `createDraftPost` and `updateDraftPost` are
  implemented.  These map to the plugin's `/posts/draft` and `/posts/<id>`
  endpoints respectively.  You can extend the bridge to support other
  draft‑only endpoints (e.g. pages, media uploads) by following the
  structure in `main.py`.

## Environment Variables

Before running or deploying the service you must set the following
environment variables:

| Variable         | Required | Description |
|------------------|----------|-------------|
| `WP_BASE_URL`    | Yes      | Base URL of the WordPress Content Agent Connector plugin. Should include the `/v1` suffix, for example: `https://greatersupport.com.au/wp-json/greater-support-content/v1`. |
| `WP_USERNAME`    | Yes      | Username for a dedicated WordPress account with Editor role permissions. |
| `WP_APP_PASSWORD` | Yes      | Application password for the dedicated WordPress user. Generate this from the WordPress admin profile page. |
| `MCP_API_KEY`    | Yes      | Shared secret used to authenticate incoming MCP requests. Agents must send this in the `Authorization` header as `Bearer <your key>`. |

## Running Locally

First, install the dependencies:

```bash
pip install -r requirements.txt
```

Then export the required environment variables and run the application using
Uvicorn:

```bash
export WP_BASE_URL="https://greatersupport.com.au/wp-json/greater-support-content/v1"
export WP_USERNAME="<your editor username>"
export WP_APP_PASSWORD="<your application password>"
export MCP_API_KEY="<a strong random secret>"

uvicorn main:app --host 0.0.0.0 --port 8000
```

Once running, the service exposes three endpoints:

- `GET /health` – forwards to `/health` on the WordPress plugin.
- `GET /capabilities` – forwards to `/capabilities` on the WordPress plugin.
- `POST /sse` – accepts JSON payloads from your agent and streams back
  results via Server‑Sent Events.  The request must include `Authorization:
  Bearer <MCP_API_KEY>`.

### Example `createDraftPost` request

Send a POST request to `/sse` with a JSON body like:

```json
{
  "action": "createDraftPost",
  "data": {
    "title": "Test post via MCP",
    "content": "This is a draft created via the MCP bridge."
  }
}
```

The service will call `POST /posts/draft` on the WordPress plugin and
stream back the result.

## Deploying

You can deploy this service on any platform that supports ASGI applications,
such as Render, Vercel, Fly.io, Railway or Heroku.  A typical deployment
workflow looks like:

1. **Create a new web service** on your hosting platform.
2. Set the Python runtime (for example, Python 3.11).
3. Set the `start` command to run Uvicorn:

   ```bash
   uvicorn mcp_bridge.main:app --host 0.0.0.0 --port $PORT
   ```

4. Define the required environment variables in your hosting platform's
   environment configuration:
   - `WP_BASE_URL`: `https://greatersupport.com.au/wp-json/greater-support-content/v1`
   - `WP_USERNAME`: **your dedicated WordPress editor username**
   - `WP_APP_PASSWORD`: **the application password for that user**
   - `MCP_API_KEY`: **a new secret token**
5. Deploy and note the public URL of your service (e.g.
   `https://mcp.yourdomain.com`).
6. The MCP endpoint must end in `/sse`, so the full URL might be
   `https://mcp.yourdomain.com/sse`.

## Connecting to OpenAI Agent Builder

Once deployed, create a new version of your agent workflow in Agent Builder.
After the human approval node, insert an **MCP Tool** and set:

- **MCP Server URL:** the full path to your deployed `/sse` endpoint
  (e.g. `https://mcp.yourdomain.com/sse`).
- **Authentication:** API Key.  Enter the same `MCP_API_KEY` that you set on
  the server.

Map actions in the tool configuration:

- `createDraftPost` → `POST` `/posts/draft`
- `updateDraftPost` → `PUT` `/posts/{id}`

For additional actions (e.g. draft pages or media uploads), extend
`main.py` following the same pattern.

## License

This project is provided for demonstration purposes.  See the
`LICENSE` file for details.