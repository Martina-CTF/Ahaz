import logging
import threading
import time
from typing import Callable

import docker
import rich
import rich.ansi
import rich.style
from ahaz_common.task import Pod, Task
from docker.errors import BuildError, NotFound
from docker.models.containers import Container
from rich.status import Status

from .task import normalise_task_name

log = logging.getLogger(__name__)


def calculate_string_colour(s: str) -> int:
    """Calculate a consistent colour code for a given string."""
    hash_value = 0
    for char in s:
        hash_value = (hash_value * 31 + ord(char)) & 0xFFFFFFFF
    return 16 + (hash_value % 216)  # Use colours from 16 to 231


def number_to_hex_colour(n: int) -> str:
    """Convert a number to a hex colour code."""
    r = (n >> 16) & 0xFF
    g = (n >> 8) & 0xFF
    b = n & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


class _ContainerLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        if not self.extra:
            self.extra = {}
        cname: str = self.extra.get("container", "<unknown>")  # type: ignore
        colour = calculate_string_colour(cname)
        return f"[bold {number_to_hex_colour(colour)}]{cname}[/] [dim]|[/] {msg}", kwargs


def get_container_name(task_name: str, pod_k8s_name: str) -> str:
    return f"ahaz-{normalise_task_name(task_name)}-{pod_k8s_name}"


def get_network_name(task_name: str, network_netname: str) -> str:
    return f"ahaz-{normalise_task_name(task_name)}-{network_netname}"


def try_build_image(image_tag: str, build_context: str, build_args: dict[str, str], verbose: bool) -> None:
    client = docker.from_env()
    log.info(f"Building image '{image_tag}'...")
    try:
        build_logs = client.api.build(
            path=build_context,
            tag=image_tag,
            buildargs=build_args,
            decode=True,
        )
        print("\x1b[2m")
        for chunk in build_logs:
            if "stream" in chunk and verbose:
                # dim the build output
                print(f"{chunk['stream']}", end="")
        print("\x1b[0m")
    except BuildError as e:
        raise e


def config_units_to_docker_units(config_value: str) -> str:
    """
    Convert resource limit strings from config format to Docker-compatible format.
    E.g., "512Mi" -> "512m", "2Gi" -> "2048m"
    """
    if config_value.endswith("Mi"):
        return config_value[:-2] + "m"
    elif config_value.endswith("Gi"):
        gi_value = int(config_value[:-2])
        return str(gi_value * 1024) + "m"
    else:
        raise ValueError(f"Unsupported resource unit in value: {config_value}")


def create_env(task: Task) -> list[tuple[Pod, Container]]:
    client = docker.from_env()
    try:
        with Status("Setting up task environment...", spinner="dots") as status:
            # Set up networks
            network_guide = {}
            for net in task.networks:
                log.info(f"Setting up network '{net.name}'...")
                status.update(f"Setting up network '{net.name}'...")
                network = client.networks.create(
                    name=f"{get_network_name(task.name, net.name)}", check_duplicate=True
                )
                for device in net.devices:
                    network_guide.setdefault(device, []).append(network)

            status.update("Setting up containers...")
            containers = []
            for pod in task.pods:
                status.update(f"Creating container for pod '{pod.name}'...")

                image_tag = f"{pod.image.image_name}:{task.version}"
                env_vars = {env.name: env.value for env in task.env_vars or [] if env.pod_name == pod.name}
                testing = getattr(pod, "testing", None)
                exposed_ports = {
                    port.split(":")[1]: int(port.split(":")[0])
                    for port in (testing.exposed_ports if testing and testing.exposed_ports else [])
                }

                container = client.containers.run(
                    image=image_tag,
                    name=f"{get_container_name(task.name, pod.name)}",
                    detach=True,
                    tty=True,
                    stdin_open=True,
                    environment=env_vars,
                    mem_limit=config_units_to_docker_units(pod.limits_ram),
                    cpu_count=pod.limits_cpu,
                    ports=exposed_ports,
                    hostname=pod.name,
                    stream=True,
                )
                containers.append((pod, container))

                # Connect to networks
                for network in network_guide.get(pod.name, []):
                    status.update(f"Connecting pod '{pod.name}' to network '{network.name}'...")
                    network.connect(container)

            status.update("Task environment set up successfully.")
            log.info("Task environment set up successfully.")
    except Exception as e:
        # Clean up any created containers and networks
        log.error(f"Error setting up task environment: {e}")
        cleanup_env(task.name, [pod.name for pod in task.pods], [net.name for net in task.networks])
        raise e
    return containers


def cleanup_env(task_name: str, pod_names: list[str], network_names: list[str]):
    client = docker.from_env()
    log.info("Cleaning up created containers and networks...")
    for pod_name in pod_names:
        container_name = get_container_name(task_name, pod_name)
        log.info(f"Cleaning up container '{container_name}'...")
        try:
            container = client.containers.get(container_name)
            container.stop()
            container.remove()
        except NotFound:
            log.warning(f"Container '{container_name}' not found during cleanup.")
            pass  # Container does not exist, nothing to clean up
    for network_name in network_names:
        network_name_full = get_network_name(task_name, network_name)
        log.info(f"Cleaning up network '{network_name_full}'...")
        try:
            network = client.networks.get(network_name_full)
            network.remove()
        except NotFound:
            log.warning(f"Network '{network_name_full}' not found during cleanup.")
            pass  # Network does not exist, nothing to clean up
    log.info("Docker test environment cleaned up successfully!")


def stream_logs(pod: Pod, container: Container, logger: _ContainerLoggerAdapter) -> None:
    while True:
        try:
            buffer = ""
            for chunk in container.logs(stream=True, tail=0, follow=True):
                # chunk can be bytes, str, or a (stdout, stderr) tuple
                if isinstance(chunk, tuple):
                    chunk = chunk[0] or chunk[1]
                if isinstance(chunk, bytes):
                    buffer += chunk.decode("utf-8", errors="replace")
                else:
                    buffer += str(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        ansi_line = rich.ansi.AnsiDecoder().decode(line)
                        for segment in ansi_line:
                            logger.info(segment.plain)
        except Exception as e:
            log.debug(f"Log stream for pod '{pod.name}' ended: {e}")


def log_docker_logs(containers: list[tuple[Pod, Container]], exit_callback: Callable) -> None:
    # Hang until the user interrupts
    log.info("Task environment is running. Press [bold]Ctrl+C[/bold] to stop.")
    print()
    # Create loggers for each container
    loggers: dict[str, _ContainerLoggerAdapter] = {}
    for pod, _ in containers:
        logger = logging.getLogger(pod.name)
        loggers[pod.name] = _ContainerLoggerAdapter(logger, {"container": pod.name})
    try:
        # Fetch logs from containers
        # TODO: I am fairly certain this allows the threads to log also the container shutdown logs,
        # but I have not tested this
        threads = []
        for pod, container in containers:
            t = threading.Thread(target=stream_logs, args=(pod, container, loggers[pod.name]), daemon=True)
            t.start()
            threads.append(t)

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        exit_callback()
