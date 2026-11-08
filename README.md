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


## Expanded troubleshooting evidence

Additional read-only tools:

- `get_pod_details(pod_name, namespace="default")`: current and previous
  container states, termination reason/exit code/signal/timestamps, images,
  memory/CPU requests and limits, conditions, owners, QoS, service account,
  image pull Secret names, and regular/init/ephemeral container details.
- `get_deployment_details(deployment_name, namespace="default")`: desired and
  observed generation, rollout conditions, replica counts, template images,
  and configured regular/init container resources.
- `get_node_details(node_name)`: conditions including MemoryPressure, resource
  capacity/allocatable, taints, and system information. Capacity/allocatable
  are not live usage measurements.

`list_pods` and `list_deployments` now include these richer summaries.
`get_pod_logs_and_events` also returns `pod_details`, independently of whether
logs are empty or unavailable. Select `previous=True` for the prior instance
and a container name for regular or init-container logs. `log_details` identifies
empty log responses. An events API error is returned as `events_error` while
preserving pod details and logs. Missing status fields remain unknown (`null`).

No environment variable values, Secret contents, kubeconfig credentials, or
container commands are included in these detail payloads. Application logs and
termination messages remain whatever the application/runtime produced.

Restart the MCP server after changing code, then restart the chat application
so tool discovery includes the current tool set. Reads require the corresponding Kubernetes RBAC permissions for pods,
pod logs, events, deployments, and nodes; partial permission failures are errors,
not evidence of a healthy resource.

## Approved Deployment remediation

The preview tool reads a specific Deployment container and reports the before
and after memory or image settings. The apply tool requires an HMAC signature
from the chat approval service. Configure a shared REMEDIATION_SIGNING_KEY in
MCP .env.mcp and the chat app .env. Use a different value from MCP_AUTH_TOKEN.

Patching requires get and patch on apps/deployments in the target namespace. The write is conditional on the reviewed cluster, object UID
and generation, with a resourceVersion check on the patch. Keep the signing key
private and use HTTPS or a private encrypted network for a remote MCP server.

The same preview and signed apply tools support single-container Deployment
creation in an existing namespace. Creation requires get on namespaces and
get/create on apps/deployments in the target namespace. The preview returns the
full manifest and expires after 30 minutes. Kubernetes create semantics protect
existing workloads with the same name.
