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



def _plain(value):
    return value.to_dict() if value is not None and hasattr(value, 'to_dict') else value


def _state(state):
    if state is None:
        return None
    if state.waiting:
        return {'waiting': {'reason': state.waiting.reason, 'message': state.waiting.message}}
    if state.terminated:
        item = state.terminated
        return {'terminated': {'reason': item.reason, 'exit_code': item.exit_code,
                               'signal': item.signal, 'message': item.message,
                               'started_at': item.started_at, 'finished_at': item.finished_at}}
    if state.running:
        return {'running': {'started_at': state.running.started_at}}
    return {}


def _containers(specs, statuses):
    indexed = {status.name: status for status in statuses or []}
    result = []
    for spec in specs or []:
        status = indexed.get(spec.name)
        resources = getattr(spec, 'resources', None)
        result.append({
            'name': spec.name, 'image': spec.image,
            'image_pull_policy': spec.image_pull_policy,
            'resources': {'requests': resources.requests or {}, 'limits': resources.limits or {}}
                         if resources else {'requests': {}, 'limits': {}},
            'ready': status.ready if status else None,
            'restarts': status.restart_count if status else None,
            'started': getattr(status, 'started', None),
            'allocated_resources': getattr(status, 'allocated_resources', None),
            'reported_resources': _plain(getattr(status, 'resources', None)),
            'state': _state(status.state) if status else None,
            'last_state': _state(status.last_state) if status else None,
            'waiting_reason': status.state.waiting.reason
                              if status and status.state and status.state.waiting else None,
            'image_id': status.image_id if status else None,
        })
    return result


def _pod_details(pod):
    return {
        'name': pod.metadata.name, 'namespace': pod.metadata.namespace,
        'uid': pod.metadata.uid, 'created_at': pod.metadata.creation_timestamp,
        'deletion_timestamp': pod.metadata.deletion_timestamp,
        'owners': [{'kind': owner.kind, 'name': owner.name, 'uid': owner.uid,
                    'controller': owner.controller} for owner in pod.metadata.owner_references or []],
        'phase': pod.status.phase, 'reason': pod.status.reason, 'message': pod.status.message,
        'node': pod.spec.node_name, 'pod_ip': pod.status.pod_ip,
        'qos_class': pod.status.qos_class, 'restart_policy': pod.spec.restart_policy,
        'service_account': pod.spec.service_account_name,
        'image_pull_secrets': [secret.name for secret in pod.spec.image_pull_secrets or []],
        'conditions': [_plain(condition) for condition in pod.status.conditions or []],
        'containers': _containers(pod.spec.containers, pod.status.container_statuses),
        'init_containers': _containers(pod.spec.init_containers, pod.status.init_container_statuses),
        'ephemeral_containers': _containers(getattr(pod.spec, 'ephemeral_containers', None),
                                             getattr(pod.status, 'ephemeral_container_statuses', None)),
    }


def _deployment_details(deployment):
    return {
        'name': deployment.metadata.name, 'namespace': deployment.metadata.namespace,
        'generation': deployment.metadata.generation,
        'observed_generation': deployment.status.observed_generation,
        'desired': deployment.spec.replicas, 'ready': deployment.status.ready_replicas or 0,
        'available': deployment.status.available_replicas or 0,
        'updated': deployment.status.updated_replicas or 0,
        'unavailable': deployment.status.unavailable_replicas or 0,
        'conditions': [_plain(item) for item in deployment.status.conditions or []],
        'containers': _containers(deployment.spec.template.spec.containers, []),
        'init_containers': _containers(deployment.spec.template.spec.init_containers, []),
    }


def get_pod_details(pod_name: str, namespace: str = 'default') -> str:
    """Inspect a pod's current/previous termination reasons, exit codes, timestamps,
    memory/CPU requests and limits, images, conditions, owners, and init containers.
    Use this to confirm OOMKilled or diagnose ImagePullBackOff. Read-only.
    """
    result = {'namespace': namespace.strip() or 'default'}
    if not pod_name.strip():
        return json.dumps({'error': 'A pod name is required.'})
    with _result_client(result) as api_client:
        pod = client.CoreV1Api(api_client).read_namespaced_pod(
            name=pod_name, namespace=result['namespace'], _request_timeout=(10, 30))
        result['pod_details'] = _pod_details(pod)
    return json.dumps(result, default=str)


def get_deployment_details(deployment_name: str, namespace: str = 'default') -> str:
    """Inspect a deployment's rollout conditions, replica counts, images and
    configured container memory/CPU requests and limits. Read-only.
    """
    result = {'namespace': namespace.strip() or 'default'}
    if not deployment_name.strip():
        return json.dumps({'error': 'A deployment name is required.'})
    with _result_client(result) as api_client:
        deployment = client.AppsV1Api(api_client).read_namespaced_deployment(
            name=deployment_name, namespace=result['namespace'], _request_timeout=(10, 30))
        result['deployment'] = _deployment_details(deployment)
    return json.dumps(result, default=str)


def get_node_details(node_name: str) -> str:
    """Inspect node conditions including MemoryPressure, capacity, allocatable
    resources, taints, and system information. Does not return live memory usage.
    """
    result = {}
    if not node_name.strip():
        return json.dumps({'error': 'A node name is required.'})
    with _result_client(result) as api_client:
        node = client.CoreV1Api(api_client).read_node(name=node_name, _request_timeout=(10, 30))
        result['node'] = {
            'name': node.metadata.name, 'unschedulable': bool(node.spec.unschedulable),
            'conditions': [_plain(item) for item in node.status.conditions or []],
            'capacity': node.status.capacity, 'allocatable': node.status.allocatable,
            'taints': [_plain(item) for item in node.spec.taints or []],
            'system_info': _plain(node.status.node_info),
        }
    return json.dumps(result, default=str)


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
    return json.dumps(result, default=str)


def list_pods(namespace: str = "default") -> str:
    """List pods with container states, last termination reasons, resource limits,
    images, conditions and init-container details. Namespace defaults to default.
    """
    namespace = namespace.strip() or "default"
    result = {"namespace": namespace}
    with _result_client(result) as api_client:
        pods = client.CoreV1Api(api_client).list_namespaced_pod(
            namespace=namespace, _request_timeout=(10, 30)
        )
        result["pods"] = [_pod_details(pod) for pod in pods.items]
    return json.dumps(result, default=str)


def list_deployments(namespace: str = "default") -> str:
    """List all deployments in a namespace. Omitted or empty namespace means default."""
    namespace = namespace.strip() or "default"
    result = {"namespace": namespace}
    with _result_client(result) as api_client:
        deployments = client.AppsV1Api(api_client).list_namespaced_deployment(
            namespace=namespace, _request_timeout=(10, 30)
        )
        result["deployments"] = [_deployment_details(item) for item in deployments.items]
    return json.dumps(result, default=str)


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
    return json.dumps(result, default=str)


def get_pod_logs_and_events(
    pod_name: str, namespace: str = "default", container: str = "",
    tail_lines: int = 100, previous: bool = False,
) -> str:
    """Get a specific pod's logs and events, defaulting to namespace default.

    Returns the last 100 log lines per container by default. Omit container to
    fetch every regular container's logs. Use previous for a restarted container's
    previous logs. Includes pod termination states and resource settings even if
    logs are empty or unavailable. A named init container is also supported.
    Events are filtered to the pod's UID.
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
        result['pod_details'] = _pod_details(pod)
        result['log_details'] = {}
        containers = [container] if container else [c.name for c in pod.spec.containers]
        for name in containers:
            try:
                result["logs"][name] = api.read_namespaced_pod_log(
                    name=pod_name, namespace=namespace, container=name,
                    tail_lines=tail_lines, previous=previous,
                    _request_timeout=(10, 30),
                )
                result['log_details'][name] = {
                    'previous': previous,
                    'empty': not bool(result['logs'][name].strip()),
                    'note': 'Empty logs do not rule out an OOM kill; inspect pod_details termination states.'
                            if not result['logs'][name].strip() else None,
                }
            except (ApiException, HTTPError, OSError) as exc:
                result["logs"][name] = {"error": (
                    f"{exc.status} {exc.reason}" if isinstance(exc, ApiException)
                    else str(exc)
                )}
        try:
            events = api.list_namespaced_event(
                namespace=namespace, field_selector=f'involvedObject.uid={pod.metadata.uid}',
                _request_timeout=(10, 30))
            result['events'] = [{
                'type': event.type, 'reason': event.reason, 'message': event.message,
                'count': event.count, 'first_timestamp': event.first_timestamp,
                'last_timestamp': event.last_timestamp,
                'event_time': getattr(event, 'event_time', None),
            } for event in events.items]
        except (ApiException, HTTPError, OSError) as exc:
            result['events_error'] = (f'{exc.status} {exc.reason}'
                                      if isinstance(exc, ApiException) else str(exc))
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
