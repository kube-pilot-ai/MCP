"""Prepare and apply narrowly scoped Deployment changes."""
import hashlib
import hmac
import json
import os
import re
from time import time
from uuid import uuid4

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from .kubernetes_tools import _api_client

TIMEOUT = (5, 15)
APPROVAL_ANNOTATION = "kube-autopilot/approval-id"


def _read(api, namespace, deployment, container):
    target = api.read_namespaced_deployment(deployment, namespace, _request_timeout=TIMEOUT)
    if target.metadata.deletion_timestamp:
        raise ValueError("The Deployment is being deleted.")
    for index, item in enumerate(target.spec.template.spec.containers):
        if item.name == container:
            return target, index, item
    raise ValueError("The named container is absent from the Deployment template.")


def _resources(api_client, container):
    return api_client.sanitize_for_serialization(container.resources) or {}


def _current(container, resources, fields):
    values = {"image": container.image,
              "memory_request": (resources.get("requests") or {}).get("memory"),
              "memory_limit": (resources.get("limits") or {}).get("memory")}
    return {field: values[field] for field in fields}


def _same_settings(current, desired):
    for field, value in desired.items():
        existing = current[field]
        if existing == value:
            continue
        if field == "image" or existing is None or parse_quantity(existing) != parse_quantity(value):
            return False
    return True


def _outcome(status, target):
    rollout = target.status
    return json.dumps({
        "status": status,
        "generation": target.metadata.generation,
        "rollout": {
            "observed_generation": rollout.observed_generation,
            "desired": target.spec.replicas,
            "updated": rollout.updated_replicas or 0,
            "available": rollout.available_replicas or 0,
        },
    })


def _preview_creation(connection, api, namespace, deployment, container, desired):
    if "image" not in desired:
        raise ValueError("Provide the exact image to deploy.")
    for name in (namespace, deployment, container):
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
            raise ValueError("Use valid DNS labels for the namespace, Deployment and container.")
    namespace_state = client.CoreV1Api(connection).read_namespace(namespace, _request_timeout=TIMEOUT)
    if namespace_state.metadata.deletion_timestamp:
        raise ValueError("The target namespace is being deleted.")
    try:
        api.read_namespaced_deployment(deployment, namespace, _request_timeout=TIMEOUT)
    except ApiException as exc:
        if exc.status != 404:
            raise
    else:
        raise ValueError("That Deployment already exists. Request a change to it instead.")

    request = desired.get("memory_request", "32Mi")
    limit = desired.get("memory_limit", "128Mi")
    if parse_quantity(request) > parse_quantity(limit):
        raise ValueError("The memory request would exceed the memory limit.")
    labels = {"app": deployment}
    manifest = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": deployment, "namespace": namespace,
                     "annotations": {APPROVAL_ANNOTATION: str(uuid4())}},
        "spec": {
            "replicas": 1, "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {
                "nodeSelector": {"kubernetes.io/os": "linux"},
                "automountServiceAccountToken": False,
                "containers": [{
                    "name": container, "image": desired["image"], "imagePullPolicy": "Always",
                    "resources": {
                        "requests": {"cpu": "25m", "memory": request},
                        "limits": {"cpu": "250m", "memory": limit},
                    },
                }],
            }},
        },
    }
    return {
        "operation": "create", "cluster": connection.configuration.host,
        "namespace": namespace, "deployment": deployment, "container": container,
        "namespace_uid": namespace_state.metadata.uid, "expires_at": int(time()) + 1800,
        "before": None, "after": manifest,
    }


def _apply_creation(connection, api, plan):
    if time() >= plan["expires_at"]:
        raise ValueError("The creation preview expired. Request a fresh plan.")
    namespace = client.CoreV1Api(connection).read_namespace(plan["namespace"], _request_timeout=TIMEOUT)
    if namespace.metadata.uid != plan["namespace_uid"] or namespace.metadata.deletion_timestamp:
        raise ValueError("The target namespace changed. Request a fresh plan.")
    manifest = plan["after"]
    try:
        created = api.create_namespaced_deployment(plan["namespace"], manifest, _request_timeout=TIMEOUT)
    except ApiException as exc:
        if exc.status != 409:
            raise
        existing = api.read_namespaced_deployment(plan["deployment"], plan["namespace"], _request_timeout=TIMEOUT)
        annotations = existing.metadata.annotations or {}
        if annotations.get(APPROVAL_ANNOTATION) != manifest["metadata"]["annotations"][APPROVAL_ANNOTATION]:
            raise ValueError("A different Deployment now occupies the approved name.")
        return _outcome("already_created", existing)
    return _outcome("created", created)


def _preview_deletion(connection, api, namespace, deployment):
    target = api.read_namespaced_deployment(deployment, namespace, _request_timeout=TIMEOUT)
    if target.metadata.deletion_timestamp:
        raise ValueError("The Deployment is already being deleted.")
    return {
        "operation": "delete", "cluster": connection.configuration.host,
        "namespace": namespace, "deployment": deployment,
        "uid": target.metadata.uid, "generation": target.metadata.generation,
        "expires_at": int(time()) + 1800, "propagation_policy": "Background",
        "before": {
            "replicas": target.spec.replicas,
            "images": {item.name: item.image for item in target.spec.template.spec.containers},
        },
        "after": None,
    }


def _apply_deletion(api, plan):
    if time() >= plan["expires_at"]:
        raise ValueError("The deletion preview expired. Request a fresh plan.")
    name, namespace = plan["deployment"], plan["namespace"]
    try:
        target = api.read_namespaced_deployment(name, namespace, _request_timeout=TIMEOUT)
        if target.metadata.uid != plan["uid"] or target.metadata.generation != plan["generation"]:
            raise ValueError("The Deployment changed since review. Request a fresh plan.")
        if not target.metadata.deletion_timestamp:
            api.delete_namespaced_deployment(
                name, namespace,
                body=client.V1DeleteOptions(
                    propagation_policy=plan["propagation_policy"],
                    preconditions=client.V1Preconditions(
                        uid=plan["uid"], resource_version=target.metadata.resource_version,
                    ),
                ),
                _request_timeout=TIMEOUT,
            )
    except ApiException as exc:
        if exc.status != 404:
            raise
        return json.dumps({"status": "already_absent"})
    return json.dumps({"status": "deletion_requested"})


def preview_deployment_remediation(namespace: str, deployment: str, container: str = "",
                                   memory_request: str = "", memory_limit: str = "",
                                   image: str = "", operation: str = "patch") -> str:
    """Preview a Deployment creation, deletion, or container settings change."""
    desired = {key: value for key, value in {
        "memory_request": memory_request, "memory_limit": memory_limit, "image": image
    }.items() if value}
    try:
        if operation not in {"create", "patch", "delete"}:
            raise ValueError("Unsupported Deployment operation.")
        if operation == "delete" and (container or desired):
            raise ValueError("Deletion targets a whole Deployment. Omit container settings.")
        if operation == "patch" and (not desired or ("image" in desired and len(desired) != 1)):
            raise ValueError("Propose a memory change or an image change, one at a time.")
        if image and not re.fullmatch(r"[^\s]{1,512}", image):
            raise ValueError("Provide a concrete image reference.")
        for key in ("memory_request", "memory_limit"):
            if key in desired and not re.fullmatch(r"[1-9][0-9]{0,5}(Mi|Gi)", desired[key]):
                raise ValueError("Memory values must be positive whole Mi or Gi quantities.")
        with _api_client() as connection:
            api = client.AppsV1Api(connection)
            if operation == "create":
                return json.dumps(_preview_creation(connection, api, namespace, deployment, container, desired))
            if operation == "delete":
                return json.dumps(_preview_deletion(connection, api, namespace, deployment))
            target, _, item = _read(api, namespace, deployment, container)
            resources = _resources(connection, item)
            before = _current(item, resources, desired)
            effective = _current(item, resources, ("memory_request", "memory_limit")) | desired
            request, limit = effective["memory_request"], effective["memory_limit"]
            if request and limit and parse_quantity(request) > parse_quantity(limit):
                raise ValueError("The memory request would exceed the memory limit.")
            if _same_settings(before, desired):
                raise ValueError("The Deployment already has the proposed settings.")
            return json.dumps({
                "cluster": connection.configuration.host,
                "namespace": namespace, "deployment": deployment, "container": container,
                "uid": target.metadata.uid, "generation": target.metadata.generation,
                "before": before, "after": desired,
            })
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except ApiException as exc:
        return json.dumps({"error": f"Kubernetes preview failed (HTTP {exc.status})."})


def apply_deployment_remediation(approval: str, signature: str) -> str:
    """Apply an exact Deployment change authorized by the chat approval service."""
    secret = os.environ.get("REMEDIATION_SIGNING_KEY")
    expected = hmac.new(secret.encode(), approval.encode(), hashlib.sha256).hexdigest() if secret else ""
    if not secret or not hmac.compare_digest(expected, signature):
        return json.dumps({"error": "A valid approval signature is required."})

    plan = json.loads(approval)
    try:
        with _api_client() as connection:
            if connection.configuration.host != plan["cluster"]:
                raise ValueError("The target cluster has changed. Request a fresh plan.")
            api = client.AppsV1Api(connection)
            operation = plan.get("operation", "patch")
            if operation == "create":
                return _apply_creation(connection, api, plan)
            if operation == "delete":
                return _apply_deletion(api, plan)
            if operation != "patch":
                raise ValueError("Unsupported Deployment operation.")
            target, index, container = _read(api, plan["namespace"], plan["deployment"], plan["container"])
            if target.metadata.uid != plan["uid"]:
                raise ValueError("The Deployment was replaced. Request a fresh plan.")
            resources = _resources(connection, container)
            current = _current(container, resources, plan["after"])
            if _same_settings(current, plan["after"]):
                return _outcome("already_present", target)
            if target.metadata.generation != plan["generation"] or current != plan["before"]:
                raise ValueError("The Deployment spec changed since review. Request a fresh plan.")

            base = f"/spec/template/spec/containers/{index}"
            operations = [{"op": "test", "path": "/metadata/resourceVersion",
                           "value": target.metadata.resource_version}]
            if "image" in plan["after"]:
                operations.append({"op": "replace", "path": base + "/image",
                                   "value": plan["after"]["image"]})
            else:
                for field, value in plan["after"].items():
                    section = {"memory_request": "requests", "memory_limit": "limits"}[field]
                    resources[section] = {**(resources.get(section) or {}), "memory": value}
                operations.append({"op": "add", "path": base + "/resources", "value": resources})

            updated = api.patch_namespaced_deployment(
                plan["deployment"], plan["namespace"], operations,
                _content_type="application/json-patch+json", _request_timeout=TIMEOUT,
            )
            return _outcome("applied", updated)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except ApiException as exc:
        if not exc.status or exc.status >= 500:
            raise
        return json.dumps({"error": f"Kubernetes rejected the approved change (HTTP {exc.status}). Check permissions or request a fresh plan."})
