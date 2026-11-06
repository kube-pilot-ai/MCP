import json
import os
from contextlib import contextmanager

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError


def _api_client():
    return config.new_client_from_config(
        config_file=os.path.expanduser(
            os.environ.get("KUBECONFIG") or "~/.kube/k8s-admin.conf"
        )
    )


@contextmanager
def _result_client(result):
    """Keep SDK errors in the tool response instead of terminating the app."""
    try:
        with _api_client() as api_client:
            yield api_client
    except ConfigException as exc:
        result["error"] = f"Error loading local kubeconfig: {exc}"
    except ApiException as exc:
        result["error"] = f"Kubernetes API error: {exc.status} {exc.reason}"
    except (HTTPError, OSError) as exc:
        result["error"] = f"Error connecting to Kubernetes: {exc}"


def list_namespaces() -> str:
    """List all namespaces in the Kubernetes cluster, including their status."""
    result = {}
    with _result_client(result) as api_client:
        namespaces = client.CoreV1Api(api_client).list_namespace(
            _request_timeout=(10, 30)
        )
        result["namespaces"] = [{
            "name": namespace.metadata.name,
            "phase": namespace.status.phase if namespace.status else None,
        } for namespace in namespaces.items]
    return json.dumps(result)


def list_pods(namespace: str = "default") -> str:
    """List all pods in a namespace. Omitted or empty namespace means default."""
    namespace = namespace.strip() or "default"
    result = {"namespace": namespace}
    with _result_client(result) as api_client:
        pods = client.CoreV1Api(api_client).list_namespaced_pod(
            namespace=namespace, _request_timeout=(10, 30)
        )
        result["pods"] = [{
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "pod_ip": pod.status.pod_ip,
            "containers": [{
                "name": status.name, "ready": status.ready,
                "restarts": status.restart_count,
                "waiting_reason": status.state.waiting.reason
                if status.state and status.state.waiting else None,
            } for status in pod.status.container_statuses or []],
        } for pod in pods.items]
    return json.dumps(result)


def list_deployments(namespace: str = "default") -> str:
    """List all deployments in a namespace. Omitted or empty namespace means default."""
    namespace = namespace.strip() or "default"
    result = {"namespace": namespace}
    with _result_client(result) as api_client:
        deployments = client.AppsV1Api(api_client).list_namespaced_deployment(
            namespace=namespace, _request_timeout=(10, 30)
        )
        result["deployments"] = [{
            "name": deployment.metadata.name,
            "desired": deployment.spec.replicas,
            "ready": deployment.status.ready_replicas or 0,
            "available": deployment.status.available_replicas or 0,
            "updated": deployment.status.updated_replicas or 0,
        } for deployment in deployments.items]
    return json.dumps(result)


def list_services(namespace: str = "default") -> str:
    """List all services in a namespace. Omitted or empty namespace means default."""
    namespace = namespace.strip() or "default"
    result = {"namespace": namespace}
    with _result_client(result) as api_client:
        services = client.CoreV1Api(api_client).list_namespaced_service(
            namespace=namespace, _request_timeout=(10, 30)
        )
        result["services"] = [{
            "name": service.metadata.name,
            "type": service.spec.type,
            "cluster_ip": service.spec.cluster_ip,
            "external_ips": service.spec.external_ips or [],
            "ports": [{"port": port.port, "target_port": port.target_port,
                       "protocol": port.protocol, "node_port": port.node_port}
                      for port in service.spec.ports or []],
        } for service in services.items]
    return json.dumps(result)


def get_pod_logs_and_events(
    pod_name: str, namespace: str = "default", container: str = "",
    tail_lines: int = 100, previous: bool = False,
) -> str:
    """Get a specific pod's logs and events, defaulting to namespace default.

    Returns the last 100 log lines per container by default. Omit container to
    fetch every regular container's logs. Use previous for a restarted container's
    previous logs. Events are filtered to the pod's UID.
    """
    namespace = namespace.strip() or "default"
    if not pod_name.strip():
        return json.dumps({"error": "A pod name is required."})
    if tail_lines < 1:
        return json.dumps({"error": "tail_lines must be at least 1."})
    result = {"namespace": namespace, "pod": pod_name, "logs": {},
              "tail_lines": tail_lines, "previous": previous}
    with _result_client(result) as api_client:
        api = client.CoreV1Api(api_client)
        pod = api.read_namespaced_pod(
            name=pod_name, namespace=namespace, _request_timeout=(10, 30)
        )
        containers = [container] if container else [c.name for c in pod.spec.containers]
        for name in containers:
            try:
                result["logs"][name] = api.read_namespaced_pod_log(
                    name=pod_name, namespace=namespace, container=name,
                    tail_lines=tail_lines, previous=previous,
                    _request_timeout=(10, 30),
                )
            except (ApiException, HTTPError, OSError) as exc:
                result["logs"][name] = {"error": (
                    f"{exc.status} {exc.reason}" if isinstance(exc, ApiException)
                    else str(exc)
                )}
        events = api.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.uid={pod.metadata.uid}",
            _request_timeout=(10, 30),
        )
        result["events"] = [{
            "type": event.type, "reason": event.reason,
            "message": event.message, "count": event.count,
            "first_timestamp": event.first_timestamp,
            "last_timestamp": event.last_timestamp,
        } for event in events.items]
    return json.dumps(result, default=str)


def list_nodes() -> str:
    """List all nodes in the Kubernetes cluster selected by the local kubeconfig."""
    try:
        with config.new_client_from_config(
            config_file=os.path.expanduser(
                os.environ.get("KUBECONFIG") or "~/.kube/k8s-admin.conf"
            )
        ) as api_client:
            nodes = client.CoreV1Api(api_client).list_node(_request_timeout=(10, 30))
    except ConfigException as exc:
        return f"Error loading local kubeconfig: {exc}"
    except ApiException as exc:
        return f"Error listing Kubernetes nodes: {exc.status} {exc.reason}"
    except (HTTPError, OSError) as exc:
        return f"Error connecting to Kubernetes: {exc}"

    result = []
    for node in nodes.items:
        ready = next(
            (condition.status for condition in node.status.conditions or []
             if condition.type == "Ready"),
            "Unknown",
        )
        result.append({
            "name": node.metadata.name,
            "ready": ready,
            "unschedulable": bool(node.spec.unschedulable),
            "kubelet_version": node.status.node_info.kubelet_version
            if node.status.node_info else None,
            "addresses": [
                {"type": address.type, "address": address.address}
                for address in node.status.addresses or []
            ],
        })
    return json.dumps(result) if result else "No nodes found."
