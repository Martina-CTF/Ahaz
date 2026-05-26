import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from time import sleep

import rich.progress
from kubernetes import client, config

from .config import REGISTRY_NAME
from .subprocess import execute_into_logger

logger = logging.getLogger(__name__)


def load_kube_config():
    try:
        config.load_kube_config()
        logger.debug("Kubeconfig loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load kubeconfig: {e}")
        raise


def get_k8s_api_ip():
    # Get IP address of the controller node
    load_kube_config()
    k8s = client.CoreV1Api()
    nodes = k8s.list_node()
    if not nodes.items:
        raise RuntimeError("No nodes found in the cluster")

    controller_ip = nodes.items[0].status.addresses[0].address

    return controller_ip


def track_daemonset_rollout(namespace: str, name: str):
    load_kube_config()
    k8s = client.AppsV1Api()

    with rich.progress.Progress() as progress:
        task = progress.add_task(f"Waiting for DaemonSet {name} to be ready...", total=None)

        while True:
            ds = k8s.read_namespaced_daemon_set(name, namespace)

            if not isinstance(ds, client.V1DaemonSet) or ds.status is None:
                logger.warning(f"DaemonSet {name} status is not available yet. Retrying...")
                continue

            if (
                ds.status.number_ready is not None
                and ds.status.desired_number_scheduled > 0
                and ds.status.number_ready == ds.status.desired_number_scheduled
            ):
                break

            progress.update(
                task,
                description=(
                    f"DaemonSet {name} ready: "
                    f"{ds.status.number_ready or 0}/{ds.status.desired_number_scheduled}"
                ),
            )
        progress.update(task, description=f"DaemonSet {name} is ready!")


def track_deployment_rollout(namespace: str, name: str, target_gen: int | None = None):
    load_kube_config()
    k8s = client.AppsV1Api()

    with rich.progress.Progress() as progress:
        task = progress.add_task(f"Waiting for Deployment {name} to be ready...", total=None)

        while True:
            deploy: client.V1Deployment = k8s.read_namespaced_deployment_status(name, namespace)  # pyright: ignore[reportAssignmentType]

            if not isinstance(deploy.status, client.V1DeploymentStatus):
                logger.warning(f"Deployment {name} status is not available yet. Retrying...")
                continue

            if deploy.status.observed_generation is None or (
                target_gen is not None and deploy.status.observed_generation < target_gen
            ):
                logger.debug(f"Deployment {name} status is not up to date yet. Retrying...")
                continue

            if (
                deploy.status.ready_replicas is not None
                and deploy.status.ready_replicas == deploy.status.replicas
                and (deploy.status.terminating_replicas == 0 or deploy.status.terminating_replicas is None)
            ):
                break

            progress.update(
                task,
                description=(
                    f"Deployment {name} ready: {deploy.status.ready_replicas or 0}/{deploy.status.replicas}"
                ),
            )
        progress.update(task, description=f"Deployment {name} is ready!")


def restart_deployment(namespace: str, name: str):
    load_kube_config()
    k8s = client.AppsV1Api()
    result = k8s.patch_namespaced_deployment(
        name,
        namespace,
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now().isoformat()}
                    }
                }
            }
        },
    )

    if isinstance(result, client.V1Deployment) and result.metadata and result.metadata.generation:
        return result.metadata.generation

    return None


def is_kind_installed():
    return shutil.which("kind") is not None


def is_helm_installed():
    return shutil.which("helm") is not None


def is_kubectl_installed():
    return shutil.which("kubectl") is not None


def check_for_cluster():
    try:
        logger.info("Checking for existing Kubernetes cluster...")
        result = subprocess.run(
            ["kind", "get", "clusters"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if "ahaz-dev" in result.stdout.splitlines():
            logger.info("Existing 'ahaz-dev' cluster found.")
            return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to check for existing cluster: {e}")
        raise


def create_kind_cluster():
    try:
        logger.info("Creating kind cluster 'ahaz-dev'...")
        # Run kind create cluster and stream its output to the logger as it runs
        execute_into_logger(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                "ahaz-dev",
                "--config",
                f"{Path(__file__).resolve().parent.parent / 'assets' / 'kind-config.yml'}",
            ],
            logger,
        )
        logger.info("Kind cluster 'ahaz-dev' created successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to create kind cluster: {e}")
        raise


def delete_kind_cluster():
    try:
        logger.info("Deleting kind cluster 'ahaz-dev'...")
        execute_into_logger(["kind", "delete", "cluster", "--name", "ahaz-dev"], logger)
        logger.info("Kind cluster 'ahaz-dev' deleted successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to delete kind cluster: {e}")
        raise


def setup_local_registry_in_kind(cluster_name="ahaz-dev"):
    try:
        logger.info(f"Setting up local registry in kind cluster '{cluster_name}'...")
        # Get kind nodes
        result = subprocess.run(
            ["kind", "get", "nodes", "--name", cluster_name],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        nodes = result.stdout.splitlines()

        registry_host = f"{REGISTRY_NAME}:5000"
        certs_dir = f"/etc/containerd/certs.d/{registry_host}"

        for node in nodes:
            logger.info(f"Configuring local registry for kind node: {node}")

            # Create registry config directory inside node
            execute_into_logger(
                ["docker", "exec", node, "mkdir", "-p", certs_dir],
                logger,
            )

            # Proper containerd hosts.toml format
            registry_config = f"""
server = "http://{registry_host}"

[host."http://{registry_host}"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
"""

            execute_into_logger(
                ["docker", "exec", "-i", node, "cp", "/dev/stdin", f"{certs_dir}/hosts.toml"],
                logger,
                input=registry_config,
            )

        # Connect registry container to kind network (ignore if already connected)
        execute_into_logger(["docker", "network", "connect", "kind", REGISTRY_NAME], logger)

        logger.info("Local registry successfully configured for kind cluster.")

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to configure local registry for kind: {e}")
        raise


def install_cilium():
    try:
        logger.info("Installing Cilium CNI...")
        execute_into_logger(["helm", "repo", "add", "cilium", "https://helm.cilium.io/"], logger)

        execute_into_logger(
            [
                "helm",
                "install",
                "cilium",
                "cilium/cilium",
                "--namespace",
                "kube-system",
                "--set",
                "kubeProxyReplacement=true",
                "--set",
                f"k8sServiceHost={get_k8s_api_ip()}",
                "--set",
                "k8sServicePort=6443",
                "--set",
                "ipam.mode=kubernetes",
            ],
            logger,
        )

        track_daemonset_rollout("kube-system", "cilium")

        logger.info("Cilium installed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install Cilium: {e}")
        raise


def install_kyverno():
    try:
        logger.info("Installing Kyverno...")
        execute_into_logger(
            [
                "helm",
                "repo",
                "add",
                "kyverno",
                "https://kyverno.github.io/kyverno/",
            ],
            logger,
        )
        execute_into_logger(
            [
                "helm",
                "install",
                "kyverno",
                "kyverno/kyverno",
                "--namespace",
                "kyverno",
                "--create-namespace",
            ],
            logger,
        )

        track_deployment_rollout("kyverno", "kyverno-admission-controller")

        logger.info("Kyverno installed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install Kyverno: {e}")
        raise


def install_ahaz(chart: str):
    try:
        logger.info("Installing Ahaz...")
        execute_into_logger(
            [
                "helm",
                "install",
                "ahaz",
                chart,
                "--namespace",
                "ahaz",
                "--create-namespace",
                "--values",
                f"{Path(__file__).resolve().parent.parent / 'assets' / 'ahaz-values.yml'}",
                "--set",
                f"controller.image.repository={REGISTRY_NAME}:5000/ahaz",
                "--set",
                f"kubernetes.k8sApiServiceCidr={get_k8s_api_ip()}/32",
            ],
            logger,
        )

        track_deployment_rollout("ahaz", "ahaz")

        logger.info("Ahaz installed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install Ahaz: {e}")
        raise


def forward_ahaz_port(port: int):
    if not is_kubectl_installed():
        logger.error("kubectl is not installed or not in PATH. Port forwarding requires kubectl.")
        sys.exit(1)

    try:
        # HACK: Python Kubernetes client's portforwarding functionality is dogshit awful, so this stays.
        execute_into_logger(
            ["kubectl", "port-forward", "svc/ahaz", f"{port}:5000", "-n", "ahaz"],
            logger,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to forward Ahaz API: {e}")
        raise
    except KeyboardInterrupt:
        logger.info("Port forwarding stopped by user.")


def restart_ahaz():
    try:
        logger.info("Restarting Ahaz deployment...")
        target_gen = restart_deployment("ahaz", "ahaz")
        # HACK: I genuinely have no idea how better to give k8s time to *start* updating the deployment
        sleep(0.2)
        track_deployment_rollout("ahaz", "ahaz", target_gen=target_gen)

        logger.info("Ahaz restarted successfully!")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to restart Ahaz: {e}")
        raise
