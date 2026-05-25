import logging
import shutil
from datetime import datetime
from pathlib import Path

import docker
import docker.errors
from ahaz_devtools.lib.subprocess import execute_into_logger

from .config import REGISTRY_NAME, REGISTRY_PORT

logger = logging.getLogger(__name__)


def docker_is_available():
    try:
        client = docker.from_env()
        client.ping()
        return shutil.which("docker") is not None
    except Exception as e:
        logger.error(f"Docker is not available: {e}")
        return False


def docker_check_registry():
    logger.info("Checking for local Docker registry...")
    client = docker.from_env()
    try:
        if client.containers.get(REGISTRY_NAME):
            logger.debug("Local Docker registry is running.")
            return True
    except docker.errors.NotFound:
        logger.debug("Local Docker registry not found.")
        return False
    except Exception as e:
        logger.error(f"Error checking for local Docker registry: {e}")
        return False


def create_local_registry():
    logger.info("Creating local Docker registry...")

    if docker_check_registry():
        logger.info("Local Docker registry already exists. Skipping creation.")
        return

    client = docker.from_env()
    run_logs = client.containers.run(
        image="registry:2",
        name=REGISTRY_NAME,
        detach=True,
        restart_policy={"Name": "always"},
        ports={"5000/tcp": ("127.0.0.1", REGISTRY_PORT)},
    )
    while run_logs.status != "running":
        run_logs.reload()
    logger.debug(run_logs.logs(since=datetime.fromtimestamp(0)).decode())
    logger.info("Local Docker registry created successfully.")


def delete_local_registry():
    logger.info("Deleting local Docker registry...")

    if not docker_check_registry():
        logger.info("No local Docker registry found. Skipping deletion.")
        return

    client = docker.from_env()
    client.containers.get(REGISTRY_NAME).remove(force=True)
    logger.info("Local Docker registry deleted successfully.")


def build_and_push_ahaz_image():
    logger.info("Building Ahaz controller image...")

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    dockerfile = project_root / "Dockerfile.controller"

    # Test if the Dockerfile exists before trying to build the image
    if not dockerfile.exists():
        logger.error(
            f"Dockerfile not found at {dockerfile}. Cannot build Ahaz image, are you refactoring something?"
        )
        return

    # HACK: the Docker library doesn't do buildkit
    # not doing buildkit for some unfathomable reason breaks Ahaz
    # I love Docker <3
    execute_into_logger(
        [
            "docker",
            "build",
            "-t",
            f"localhost:{REGISTRY_PORT}/ahaz:latest",
            "-f",
            str(dockerfile),
            str(project_root),
        ],
        logger,
        log_level=logging.INFO,
    )

    logger.info("Pushing image...")

    execute_into_logger(
        [
            "docker",
            "push",
            f"localhost:{REGISTRY_PORT}/ahaz:latest",
        ],
        logger,
    )

    logger.info("Push complete.")
