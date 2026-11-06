# Kubernetes MCP server

Independent authenticated Streamable HTTP server exposing list_nodes,
list_namespaces, list_pods, list_deployments, list_services, and
get_pod_logs_and_events. Namespaced tools use default when no namespace is given.
Uses the Kubernetes Python SDK and the server machine's kubeconfig. It needs
neither an OpenAI API key nor PostgreSQL, Qdrant, or the chat application.

## Local setup (PowerShell)

```powershell
cd C:\Users\kusum\Documents\Final_Project\MCP
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Fresh clone only: Copy-Item .env.mcp.example .env.mcp, then set your values.
.\.venv\Scripts\python.exe -m mcp_server
```

Your local .env.mcp has already been migrated and remains Git-ignored.
KUBECONFIG defaults to ~/.kube/k8s-admin.conf. The endpoint is
http://127.0.0.1:8002/mcp; it is an MCP protocol endpoint, not a web page.
Clients must send Authorization: Bearer <MCP_AUTH_TOKEN>. The chat app's token
must match. Restart both services after changing it. Restart clients after
changing tool schemas.

## Remote hosting

Set MCP_HOST=0.0.0.0, keep MCP_AUTH_TOKEN configured, and set MCP_ALLOWED_HOSTS
to exact client-visible hostnames/IPs, e.g. mcp.internal.example:*,192.168.1.50:*.
Set MCP_SERVER_URL in the chat app to this server's /mcp URL. Use HTTPS through
a reverse proxy or a private encrypted network between machines. Only this
server needs Kubernetes connectivity and cluster credentials.

## Optional standalone Docker image

```powershell
docker build -t kube-pilot-mcp .
docker run --rm --name kube-pilot-mcp -p 127.0.0.1:8002:8002 --env-file .env.mcp -e MCP_HOST=0.0.0.0 -e KUBECONFIG=/root/.kube/k8s-admin.conf --mount "type=bind,source=$env:USERPROFILE\.kube,target=/root/.kube,readonly" kube-pilot-mcp
```

Choose Python or Docker on port 8002. Container certificate paths and credential
helpers must work inside Linux. No files are imported from the original repository.
