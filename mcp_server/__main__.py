import os
from pathlib import Path
from secrets import compare_digest

import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import kubernetes_tools, remediation


def create_app():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.mcp", override=True)
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise RuntimeError("Set MCP_AUTH_TOKEN before exposing the MCP server on a network interface.")
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_hosts.extend(value.strip() for value in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if value.strip())
    mcp = FastMCP(
        "Kubernetes tools", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
        ),
    )
    for name in (
        "list_nodes", "list_namespaces", "list_pods", "list_deployments",
        "list_services", "get_pod_logs_and_events",
        "get_pod_details", "get_deployment_details", "get_node_details",
    ):
        mcp.add_tool(getattr(kubernetes_tools, name))
    mcp.add_tool(remediation.preview_deployment_remediation)
    mcp.add_tool(remediation.apply_deployment_remediation)
    asgi_app = mcp.streamable_http_app()
    if not token:
        return asgi_app

    async def authenticated_app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            if not compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", b"Bearer"),
                                        (b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await asgi_app(scope, receive, send)

    return authenticated_app


def main():
    app = create_app()
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", "8002")))


if __name__ == "__main__":
    main()
