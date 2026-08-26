import functools
import json
import logging
import os
import time
import traceback

import redis.asyncio as aioredis
from ahaz_common.task import AccessEnum, PodInformation, Task
from ahaz_common.util import adapt_limit_size
from kubernetes import config, watch
from kubernetes.client import (
    CoreV1Api,
    NetworkingV1Api,
    V1Affinity,
    V1Capabilities,
    V1ConfigMap,
    V1ConfigMapVolumeSource,
    V1Container,
    V1EnvVar,
    V1HostPathVolumeSource,
    V1KeyToPath,
    V1LabelSelector,
    V1LabelSelectorRequirement,
    V1Namespace,
    V1NetworkPolicy,
    V1NetworkPolicyEgressRule,
    V1NetworkPolicyIngressRule,
    V1NetworkPolicyPeer,
    V1NetworkPolicyPort,
    V1NetworkPolicySpec,
    V1NodeAffinity,
    V1NodeSelector,
    V1NodeSelectorRequirement,
    V1NodeSelectorTerm,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1ResourceRequirements,
    V1Secret,
    V1SecurityContext,
    V1Service,
    V1ServiceAccount,
    V1ServicePort,
    V1ServiceSpec,
    V1Toleration,
    V1Volume,
    V1VolumeMount,
)
from kubernetes.client.rest import ApiException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .certmanager import (
    generate_user,
    get_down_script,
    get_openvpn_env,
    get_server_ovpn_config,
    get_server_ta,
    get_up_script,
    get_user,
)
from .db.operator import (
    get_certificate_by_common_name,
    get_only_certificate_by_common_name,
    get_task_definition,
)
from .util.container import get_image_name
from .util.misc import str_to_bool

# This file has `#type: ignore` comments to ignore type checking errors from the kubernetes client library,
# which has weird/bad type annotations.
# Woe.

logger = logging.getLogger()

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
K8S_IMAGEPULLSECRET_NAMESPACE = os.getenv("K8S_IMAGEPULLSECRET_NAMESPACE", "default")
K8S_IMAGEPULLSECRET_NAME = os.getenv("K8S_IMAGEPULLSECRET_NAME", "regcred")
CERT_DIR_CONTAINER = os.getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certdir")
OVPN_IMAGE = os.getenv("OVPN_IMAGE", "lisenet/openvpn")
OVPN_TAG = os.getenv("OVPN_TAG", "latest")


# Quick heuristic to determine if the kube folder has a valid kubeconfig file
# or merely a service account token.
def is_valid_kubeconfig(kube_folder: str) -> bool:
    # Check for the presence of a valid kubeconfig file
    kubeconfig_path = os.path.join(kube_folder, "config")
    if os.path.exists(kubeconfig_path):
        return True

    # Check for the presence of a service account token
    token_path = os.path.join(kube_folder, "token")
    if os.path.exists(token_path):
        return True

    return False


@functools.cache
def load_kube_config():
    # Load kube config based on environment
    if is_valid_kubeconfig("/.kube"):
        config.load_kube_config(config_file="/.kube/config")
    else:
        config.load_incluster_config()


def should_retry_request(exception):
    """Return True if the exception is an ApiException with a status worth retrying."""
    is_forbidden = (
        isinstance(exception, ApiException)
        and exception.status
        and (
            exception.status == 403  # Forbidden
            # theoretically a lost cause but in the case of the example deployment
            # the service account might not have the role yet
            or exception.status == 429  # Too Many Requests
            or exception.status >= 500  # Server errors
        )
    )
    return is_forbidden


def should_retry_patch(exception):
    return should_retry_request(exception) or (
        # Possible we are patching something not created yet
        isinstance(exception, ApiException) and exception.status == 404
    )


retry_opts = {
    "retry": retry_if_exception(should_retry_request),  # type: ignore
    "stop": stop_after_attempt(5),  # Stop after 5 attempts
    "wait": wait_exponential(multiplier=1, min=2, max=10),  # Exponential backoff
}


def create_network_policy_deny_all() -> V1NetworkPolicy:
    policy = V1NetworkPolicy(
        api_version="networking.k8s.io/v1",
        kind="NetworkPolicy",
        metadata=V1ObjectMeta(name="deny-all"),
        spec=V1NetworkPolicySpec(
            pod_selector=V1LabelSelector(match_labels={}),
            policy_types=["Ingress", "Egress"],
            ingress=[],
            egress=[],
        ),
    )
    return policy


def create_network_policy(team_id: str) -> V1NetworkPolicy:
    policy = V1NetworkPolicy(
        api_version="networking.k8s.io/v1",
        kind="NetworkPolicy",
        metadata=V1ObjectMeta(name="restrict-vpn-access"),
        spec=V1NetworkPolicySpec(
            pod_selector=V1LabelSelector(match_labels={"name": "vpn-container-pod"}),
            policy_types=["Ingress", "Egress"],
            ingress=[
                V1NetworkPolicyIngressRule(
                    ports=[
                        V1NetworkPolicyPort(protocol="TCP", port=1194),
                        V1NetworkPolicyPort(protocol="UDP", port=1194),
                    ]
                )
            ],
            egress=[
                # Explicitly deny all egress traffic by default
                # Allow communication only within the same namespace
                V1NetworkPolicyEgressRule(
                    to=[V1NetworkPolicyPeer(pod_selector=V1LabelSelector(match_labels={"team": team_id}))]
                )
            ],
        ),
    )
    return policy


@retry(**retry_opts)
async def start_challenge_pod(team_id: str, pod: PodInformation, task_name: str, task_version: str) -> None:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        pod_manifest = V1Pod(
            metadata=V1ObjectMeta(
                name=pod.name,
                labels={
                    "team": team_id,
                    "visible": str(pod.visible),
                    "task": task_name,
                    "name": pod.name,
                    "version": task_version,
                },
            ),
            spec=V1PodSpec(
                containers=[
                    V1Container(
                        image=f"{get_image_name(pod.image)}:{task_version}",
                        name="container",
                        env=[V1EnvVar(name=var.name, value=var.value) for var in pod.env],
                        resources=V1ResourceRequirements(
                            limits={
                                "memory": adapt_limit_size(pod.limits.ram),
                                "cpu": str(pod.limits.cpu),
                                "ephemeral-storage": adapt_limit_size(pod.limits.ephemeral_storage),
                            },
                            # Zero out requests to avoid actual scheduling issues
                            requests={
                                "memory": "0",
                                "cpu": "0",
                                "ephemeral-storage": "0",
                            },
                        ),
                    )
                ],
                tolerations=[
                    V1Toleration(key="ahaz-controller/node-role", operator="Equal", value="task"),
                    V1Toleration(key="ahaz-controller/node-role", operator="Equal", value="shared"),
                ],
                affinity=V1Affinity(
                    node_affinity=V1NodeAffinity(
                        required_during_scheduling_ignored_during_execution=V1NodeSelector(
                            node_selector_terms=[
                                V1NodeSelectorTerm(
                                    match_expressions=[
                                        V1NodeSelectorRequirement(
                                            key="ahaz-controller/node-role",
                                            operator="In",
                                            values=["task", "shared"],
                                        )
                                    ]
                                )
                            ]
                        )
                    )
                ),
                image_pull_secrets=[{"name": K8S_IMAGEPULLSECRET_NAME}],
            ),
        )

        logger.debug(f"Creating pod {pod.name} in namespace {team_id} with image {pod.image.name}")
        logger.debug(f"Pod manifest: {pod_manifest}")

        core_api.create_namespaced_pod(namespace=team_id, body=pod_manifest)
        create_pod_service(team_id, task_name, pod.name)
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when starting challenge pod: {e}")
        raise e


@retry(**retry_opts)
async def start_challenge(team_name: str, task_name: str) -> None:
    try:
        logger.info(f"Starting challenge {task_name} for team {team_name}")
        task = await get_task_definition(task_name)

        for pod in task.pods:
            version = task.version if task.version else "latest"

            await start_challenge_pod(
                team_name,
                pod,
                task_name,
                version,
            )

        await create_challenge_network_policies(task, team_name)
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when starting challenge: {e}")
        raise e
    except ValueError as e:
        logger.error(f"ValueError when starting challenge: {e}")
        raise e


async def summarise_pods_list(pod_list: V1PodList, showInvisible: bool) -> list[dict[str, str]]:
    if pod_list is None or not pod_list.items:
        return []

    pod_info = []
    for pod in pod_list.items:
        pod: V1Pod = pod

        # Test whether we have all the values we expect
        if not pod.metadata:
            logger.warning("Pod is missing metadata:")
            logger.warning(pod)
            continue

        if pod.status is None:
            logger.warning(f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} has no status.")
            continue

        # Test if pod is visible
        if "visible" in pod.metadata.labels:
            pod_visible = str_to_bool(pod.metadata.labels["visible"])
        else:
            if pod.metadata.name != "vpn-container-pod":
                logger.warning(
                    f"Pod {pod.metadata.name} in namespace {pod.metadata.namespace} missing 'visible' label."
                )
            pod_visible = True  # default to visible if label is missing

        if not pod_visible and not showInvisible:
            continue

        # Get pod status
        is_vpn = pod.metadata.name == "vpn-container-pod"

        # because python k8s api does not show status terminating :/
        if pod.metadata.deletion_timestamp is not None and pod.status.phase in ("Pending", "Running"):
            state = "Terminating"
        else:
            state = str(pod.status.phase)

        pod_data = {
            "status": state,
            "ip": pod.status.pod_ip,
            "visibleIP": pod_visible,
            "task": pod.metadata.labels["task"] if not is_vpn else None,
            "name": pod.metadata.labels["name"],
        }

        pod_info.append(pod_data)

    return pod_info


@retry(**retry_opts)
async def get_pods_namespace(team_name: str, show_invisible: bool) -> str:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        pod_list: V1PodList = core_api.list_namespaced_pod(team_name)

        if not pod_list.items:
            return json.dumps([])

        pod_info = await summarise_pods_list(pod_list, show_invisible)

        return json.dumps(pod_info)
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when getting pods in namespace {team_name}: {e}")
        raise e


@retry(**retry_opts)
def create_pod_service(team_name: str, task_name: str, pod_name: str) -> None:
    load_kube_config()
    try:
        core_api = CoreV1Api()

        service = V1Service(
            metadata=V1ObjectMeta(
                name=pod_name,
                namespace=team_name,
                labels={"task": task_name},
            ),
            spec=V1ServiceSpec(
                cluster_ip="None",  # headless service
                selector={"name": pod_name},
            ),
        )

        # Create the service in Kubernetes
        api_response: V1Service = core_api.create_namespaced_service(namespace=team_name, body=service)  # type: ignore
        logger.debug(f"Service created. Status='{api_response.status}'")
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when creating service for pod {pod_name}: {e}")
        raise e


def create_network_policy_deny_all_task(task_name: str) -> V1NetworkPolicy:
    policy = V1NetworkPolicy(
        api_version="networking.k8s.io/v1",
        kind="NetworkPolicy",
        metadata=V1ObjectMeta(name=f"{task_name}-deny-all", labels={"task": task_name}),
        spec=V1NetworkPolicySpec(
            pod_selector=V1LabelSelector(match_labels={"task": task_name}),
            policy_types=["Ingress", "Egress"],
            ingress=[],
            egress=[],
        ),
    )
    return policy


def create_network_policy_allow_task(
    task_name: str, network_pods: list[str], network_name: str
) -> V1NetworkPolicy:
    # Explicitly allow DNS
    dns_peer = V1NetworkPolicyPeer(
        namespace_selector=V1LabelSelector(match_labels={"kubernetes.io/metadata.name": "kube-system"}),
        pod_selector=V1LabelSelector(match_labels={"k8s-app": "kube-dns"}),
    )
    dns_egress_rule = V1NetworkPolicyEgressRule(
        to=[dns_peer],
        ports=[
            V1NetworkPolicyPort(protocol="UDP", port=53),
            V1NetworkPolicyPort(protocol="TCP", port=53),
        ],
    )
    # Explicitly allow the pods within the network
    pod_selector = V1LabelSelector(
        match_expressions=[V1LabelSelectorRequirement(key="name", operator="In", values=network_pods)]
    )

    peer_selector = V1NetworkPolicyPeer(
        pod_selector=V1LabelSelector(
            match_expressions=[V1LabelSelectorRequirement(key="name", operator="In", values=network_pods)]
        )
    )

    ingress_rule = V1NetworkPolicyIngressRule(_from=[peer_selector])
    egress_rule = V1NetworkPolicyEgressRule(to=[peer_selector])

    pod_selector = V1LabelSelector(
        match_expressions=[
            V1LabelSelectorRequirement(
                key="name",
                operator="In",
                values=network_pods,  # your array of pod names
            )
        ]
    )

    policy = V1NetworkPolicy(
        api_version="networking.k8s.io/v1",
        kind="NetworkPolicy",
        metadata=V1ObjectMeta(name=f"{task_name}-{network_name}-allow-all", labels={"task": task_name}),
        spec=V1NetworkPolicySpec(
            pod_selector=pod_selector,
            policy_types=["Ingress", "Egress"],
            ingress=[ingress_rule],
            egress=[dns_egress_rule, egress_rule],
        ),
    )
    return policy


@retry(**retry_opts)
async def create_challenge_network_policies(task: Task, team_id: str) -> None:
    load_kube_config()
    try:
        net_api = NetworkingV1Api()
        deny_policy = create_network_policy_deny_all_task(task.name)
        net_api.create_namespaced_network_policy(namespace=team_id, body=deny_policy)

        for network in task.networks:
            network_pods = [x.name for x in task.pods if network.name in x.networks]

            if AccessEnum.player in network.access:  # if it is teamnet, include the vpn pod in whitelist
                network_pods.append("vpn-container-pod")

            allow_policy = create_network_policy_allow_task(task.name, network_pods, network.name)
            net_api.create_namespaced_network_policy(namespace=team_id, body=allow_policy)

    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when creating challenge network policies for {task.name}: {e}")
        raise e


@retry(**retry_opts)
def stop_challenge(team_id: str, task_name: str) -> str:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        net_api = NetworkingV1Api()

        label_selector = f"task={task_name}"

        # Delete Pods
        pod_list: V1PodList = core_api.list_namespaced_pod(namespace=team_id, label_selector=label_selector)

        if not pod_list.items:
            logger.info(f"No pods found with label task={task_name} in namespace {team_id}")
        else:
            for pod in pod_list.items:
                if not pod.metadata:
                    logger.warning(f"Pod in namespace {team_id} is missing metadata.")
                    continue
                logger.debug(f"Deleting Pod: {pod.metadata.name}")
                core_api.delete_namespaced_pod(name=pod.metadata.name, namespace=team_id)

        # Delete Services
        services = core_api.list_namespaced_service(namespace=team_id, label_selector=label_selector)
        for svc in services.items:
            logger.debug(f"Deleting Service: {svc.metadata.name}")
            core_api.delete_namespaced_service(name=svc.metadata.name, namespace=team_id)

        # Delete NetworkPolicies
        policies = net_api.list_namespaced_network_policy(namespace=team_id, label_selector=label_selector)
        for policy in policies.items:
            logger.info(f"Deleting NetworkPolicy: {policy.metadata.name}")
            net_api.delete_namespaced_network_policy(name=policy.metadata.name, namespace=team_id)

        logger.info(f"All resources with label task={task_name} deleted from namespace {team_id}")
        return f"All resources with label task={task_name} deleted from namespace {team_id}"
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when stopping challenge {task_name} in namespace {team_id}: {e}")
        raise e


@retry(**retry_opts)
def create_secret_in_namespace(team_id: str, secret_data: V1Secret) -> None:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        core_api.create_namespaced_secret(namespace=team_id, body=secret_data)
        logger.debug(f"Created secret {secret_data.metadata.name} in namespace {team_id}")  # type: ignore
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when creating secret in namespace {team_id}: {e}")
        else:
            logger.debug(f"API Exception when creating secret in namespace {team_id}: {e}")
        raise e


@retry(**retry_opts)
def check_namespaced_service_account_exists(namespace: str, service_account_name: str) -> bool:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        core_api.read_namespaced_service_account(name=service_account_name, namespace=namespace)
        logger.debug(f"Service account {service_account_name} exists in namespace {namespace}")
        return True
    except ApiException as e:
        if e.status == 404:
            logger.debug(f"Service account {service_account_name} does not exist in namespace {namespace}")
            return False
        elif e.status != 403:
            logger.error(
                f"API Exception when checking service account {service_account_name} "
                + f"in namespace {namespace}: {e}",
            )
        else:
            logger.debug(
                f"API Exception when checking service account {service_account_name} "
                + f"in namespace {namespace}: {e}",
            )
        raise e


patch_retry_opts = {
    **retry_opts,
    "retry": retry_if_exception(should_retry_patch),  # type: ignore
}


@retry(**patch_retry_opts)
def patch_namespaced_service_account(
    namespace: str, service_account_name: str, body: V1ServiceAccount
) -> None:
    load_kube_config()

    try:
        core_api = CoreV1Api()
        core_api.patch_namespaced_service_account(name=service_account_name, namespace=namespace, body=body)
        logger.debug(f"Patched service account {service_account_name} in namespace {namespace}")
    except ApiException as e:
        if e.status not in (403, 404):
            logger.error(
                f"API Exception when patching service account {service_account_name} "
                + f"in namespace {namespace}: {e}",
            )
        elif e.status == 404:
            logger.warning(
                f"Service account {service_account_name} not found in namespace {namespace}"
                + f" when patching: {e}",
            )
        else:
            logger.debug(
                f"API Exception when patching service account {service_account_name}"
                + f" in namespace {namespace}: {e}",
            )
        raise e


# old functions from old controller
@retry(**retry_opts)
def create_team_namespace(team_id: str) -> None:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        logger.debug(f"Creating namespace {team_id}")
        core_api.create_namespace(V1Namespace(metadata=V1ObjectMeta(name=team_id)))
        logger.debug(f"Moving regcred into namespace {team_id}")

        regcred: V1Secret = core_api.read_namespaced_secret(
            name=K8S_IMAGEPULLSECRET_NAME, namespace=K8S_IMAGEPULLSECRET_NAMESPACE
        )  # type: ignore

        if not regcred.metadata:
            logger.error(f"Secret {K8S_IMAGEPULLSECRET_NAME} is missing metadata.")
            raise Exception(f"Secret {K8S_IMAGEPULLSECRET_NAME} is missing metadata.")

        regcred.metadata.namespace = team_id
        regcred.metadata.resource_version = None
        create_secret_in_namespace(team_id, regcred)
        # patch the default service account to disallow auto-mounting of the token
        patch_namespaced_service_account(
            namespace=team_id,
            service_account_name="default",
            body=V1ServiceAccount(automount_service_account_token=False),
        )
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when creating namespace {team_id}: {e}")
        else:
            logger.debug(f"API Exception when creating namespace {team_id}: {e}")
        raise e
    except Exception as e:
        logger.error(f"General Exception when creating namespace {team_id}: {e}")
        raise e


@retry(**retry_opts)
async def create_team_vpn_configmap(team_id) -> None:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        teamCertDir = CERT_DIR_CONTAINER + team_id

        ovpn_config = get_server_ovpn_config(teamCertDir)
        
        try:
            server_cert = await get_certificate_by_common_name(f"server.{team_id}.{PUBLIC_DOMAINNAME}")
            ca = await get_only_certificate_by_common_name(f"ca.{team_id}.{PUBLIC_DOMAINNAME}")
        except ValueError as e:
            logger.error(f"Error retrieving certificates for team {team_id}: {e}")
            raise e

        server_ta = get_server_ta(teamCertDir)
        ovpn_env = get_openvpn_env(teamCertDir)
        up_script = get_up_script(teamCertDir)
        down_script = get_down_script(teamCertDir)

        config_map = V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=V1ObjectMeta(name=f"{team_id}-vpn-config"),
            data={
                "ovpn.conf": ovpn_config,
                "server.key": server_cert.get_private_key_pem(),
                "server.crt": server_cert.get_certificate_pem(),
                "ca.crt": ca,
                "ta.key": server_ta,
                "ovpn.env": ovpn_env,
                "up.sh": up_script,
                "down.sh": down_script,
            },
        )

        core_api.create_namespaced_config_map(namespace=team_id, body=config_map)
        logger.debug(f"Created ConfigMap vpn-config-{team_id} in namespace {team_id}")
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when creating VPN ConfigMap for team {team_id}: {e}")
        raise e


@retry(**retry_opts)
async def create_team_vpn_container(team_id: str) -> None:
    load_kube_config()
    try:
        await create_team_vpn_configmap(team_id)
        core_api = CoreV1Api()
        pod_manifest = V1Pod(
            metadata=V1ObjectMeta(
                name="vpn-container-pod",
                labels={"name": "vpn-container-pod", "team": team_id},
            ),
            spec=V1PodSpec(
                containers=[
                    V1Container(
                        image=f"{OVPN_IMAGE}:{OVPN_TAG}",
                        name="vpn-container",
                        volume_mounts=[
                            V1VolumeMount(
                                mount_path="/etc/openvpn",
                                name="vpn-volume",
                                read_only=True,  # might need to be changed later
                            ),
                            V1VolumeMount(mount_path="/dev/net/tun", name="dev-net-tun", read_only=False),
                        ],
                        # NOTE: NET_ADMIN is required for OpenVPN function
                        security_context=V1SecurityContext(capabilities=V1Capabilities(add=["NET_ADMIN"])),
                        env=[V1EnvVar(name="DEBUG", value="1")],
                    )
                ],
                volumes=[
                    V1Volume(
                        name="vpn-volume",
                        config_map=V1ConfigMapVolumeSource(
                            name=f"{team_id}-vpn-config",
                            items=[
                                V1KeyToPath(key="ovpn.conf", path="openvpn.conf"),
                                V1KeyToPath(key="server.key", path="pki/private/server.key"),
                                V1KeyToPath(key="server.crt", path="pki/issued/server.crt"),
                                V1KeyToPath(key="ca.crt", path="pki/ca.crt"),
                                V1KeyToPath(key="ta.key", path="pki/ta.key"),
                                V1KeyToPath(key="ovpn.env", path="ovpn_env.sh"),
                                V1KeyToPath(key="up.sh", path="up.sh"),
                                V1KeyToPath(key="down.sh", path="down.sh"),
                            ],
                        ),
                    ),
                    V1Volume(name="dev-net-tun", host_path=V1HostPathVolumeSource(path="/dev/net/tun")),
                ],
                tolerations=[
                    V1Toleration(key="ahaz-controller/node-role", operator="Equal", value="vpn"),
                    V1Toleration(key="ahaz-controller/node-role", operator="Equal", value="shared"),
                ],
                affinity=V1Affinity(
                    node_affinity=V1NodeAffinity(
                        required_during_scheduling_ignored_during_execution=V1NodeSelector(
                            node_selector_terms=[
                                V1NodeSelectorTerm(
                                    match_expressions=[
                                        V1NodeSelectorRequirement(
                                            key="ahaz-controller/node-role",
                                            operator="In",
                                            values=["vpn", "shared"],
                                        )
                                    ]
                                )
                            ]
                        )
                    )
                ),
            ),
        )
        core_api.create_namespaced_pod(body=pod_manifest, namespace=team_id)
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when creating VPN container for team {team_id}: {e}")
        raise e


@retry(**retry_opts)
def expose_team_vpn_container(team_id: str, port: int) -> None:
    load_kube_config()
    try:
        logger.info(f"Exposing VPN container for team {team_id} on port {port}")
        core_api = CoreV1Api()
        service = V1Service(
            metadata=V1ObjectMeta(
                name="vpn-container-service",
                namespace=team_id,
            ),
            spec=V1ServiceSpec(
                selector={"name": "vpn-container-pod"},  # Selector to match the pod labels
                ports=[
                    V1ServicePort(
                        port=1194,  # Port exposed by the service (VPN port)
                        target_port=1194,  # Container's port
                        node_port=port,  # NodePort; k8s will allocate one if not specified
                    )
                ],
                type="NodePort",  # Service type is NodePort
            ),
        )
        api_service_response: V1Service = core_api.create_namespaced_service(
            namespace=team_id,
            body=service,
        )  # type: ignore
        logger.debug(f"Service created. Status: '{api_service_response.status}'")

        policy_deny = create_network_policy_deny_all()
        policy = create_network_policy(team_id)
        logger.debug("The following network policies will be applied:")
        logger.debug(f"Deny-all policy: {policy_deny}")
        logger.debug(f"Restrict-vpn-access policy: {policy}")

        net_api = NetworkingV1Api()
        logger.debug("Applying network policies...")
        api_network_response: V1NetworkPolicy = net_api.create_namespaced_network_policy(
            namespace=team_id, body=policy
        )  # type: ignore
        logger.debug(f"Restrict-vpn-access policy created. Status: '{api_network_response}'")

        api_network_response_deny: V1NetworkPolicy = net_api.create_namespaced_network_policy(
            namespace=team_id, body=policy_deny
        )  # type: ignore
        logger.debug(f"Deny-all policy created. Status: '{api_network_response_deny}'")
        logger.debug("Successfully applied network policy")
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when exposing VPN container for team {team_id}: {e}")
        raise e


# TODO: Remove this and just call generate_user directly? Certificate rework will not need this, tho.
async def register_user_ovpn(team_id: str, user_id: str) -> str:
    vpnDirLocation = CERT_DIR_CONTAINER + team_id
    await generate_user(team_id, user_id, vpnDirLocation)
    return "successfully registered"


# TODO: Remove this, nothing calls it.
async def obtain_user_ovpn_config(team_id: str, user_id: str) -> str:
    vpnDirLocation = CERT_DIR_CONTAINER + team_id
    result = await get_user(team_id, user_id, vpnDirLocation)
    result = str(result).replace("\\n", "\n")
    return result


# FIXME: I am unused! Probably will be used when team deletion is implemented.
def delete_namespace(team_id: str, timeout: int = 300, interval: int = 5) -> int:
    load_kube_config()
    try:
        core_api = CoreV1Api()
        try:
            logger.info(f"Deleting namespace: {team_id}")
            core_api.delete_namespace(name=team_id)
        except ApiException as e:
            if e.status == 404:
                logger.info(f"Namespace {team_id} does not exist.")
                return 0
            else:
                logger.error(f"Error deleting namespace: {e}")
                return 1

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                ns = core_api.read_namespace(name=team_id)

                # If namespace is stuck terminating → remove finalizers
                if ns.metadata.deletion_timestamp and ns.spec.finalizers:  # type: ignore
                    logger.debug(f"Namespace {team_id} stuck in Terminating, removing finalizers...")
                    body = V1Namespace(metadata=V1ObjectMeta(finalizers=[]))
                    try:
                        core_api.patch_namespace(name=team_id, body=body)
                    except ApiException as e:
                        logger.error(f"Failed to patch namespace finalizers: {e}")
                        return 1

                logger.debug(f"Namespace {team_id} still exists. Waiting {interval}s...")

            except ApiException as e:
                if e.status == 404:
                    logger.info(f"Namespace {team_id} successfully deleted.")
                    return 0
                else:
                    logger.error(f"Unexpected error while checking namespace: {e}")
                    return 1

            time.sleep(interval)

        logger.error(f"Timeout: Namespace {team_id} not deleted after {timeout} seconds.")
        return 1
    except ApiException as e:
        if e.status != 403:
            logger.error(f"API Exception when deleting namespace {team_id}: {e}")
        raise e


async def k8s_watcher(redis_client: aioredis.Redis) -> None:
    load_kube_config()
    core_api = CoreV1Api()
    w = watch.Watch()
    logger.info("Starting Kubernetes watcher...")
    for event_untyped in w.stream(core_api.list_pod_for_all_namespaces):
        try:
            event: V1PodList = event_untyped  # type: ignore

            # Publish pod name, labels, status, ip to the event manager
            pod: V1Pod = event["object"]  # type: ignore
            event_type: str = event["type"]  # type: ignore
            pod_name: str = pod.metadata.name if pod.metadata and pod.metadata.name else "unknown"
            pod_namespace: str = (
                pod.metadata.namespace if pod.metadata and pod.metadata.namespace else "unknown"
            )
            pod_labels: dict[str, str] = pod.metadata.labels if pod.metadata and pod.metadata.labels else {}
            pod_status: str = pod.status.phase if pod.status and pod.status.phase else "unknown"
            pod_ip: str = pod.status.pod_ip if pod.status and pod.status.pod_ip else "unknown"

            challenge_name: str | None = None

            if "task" in pod_labels:
                try:
                    challenge_name = pod_labels["task"]
                except Exception:
                    pass

            if pod_status == "Failed":
                # Hide failed status. It shows up for a split second when pod is deleted.
                pod_status = "Terminating"

            if (pod.metadata.deletion_timestamp if pod.metadata else None) is not None and pod_status in (
                "Pending",
                "Running",
            ):
                pod_status = "Terminating"

            event_data = {
                "event_type": event_type,
                "pod_name": pod_name,
                "pod_namespace": pod_namespace,
                "pod_status": pod_status,
                "pod_ip": pod_ip,
                "visible": str_to_bool(pod_labels.get("visible", "False")),
                "challenge": challenge_name,
            }

            await redis_client.publish("ahaz_events", json.dumps({"type": "pod_event", "data": event_data}))
        except Exception as e:
            logger.error("Error processing Kubernetes event:")
            logger.error(e)
            logger.error(traceback.format_exc())
            continue
