import colorsys
import logging
import time
from pathlib import Path
from typing import Annotated

import docker
import docker.errors
import typer
from rich.status import Status

from .lib.docker import cleanup_env, create_env, log_docker_logs, try_build_image
from .lib.file import test_for_file
from .lib.task import deserialise_task, normalise_task_name
from .templates import copy_example_images, write_task_yaml

log = logging.getLogger(__name__)
CWD = Path.cwd()


def test(
    build: Annotated[bool, typer.Option("--build", "-b", help="Always build Docker images.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose logging.")] = False,
    up: Annotated[bool, typer.Option("--up", "-u", help="Start the task environment after testing.")] = False,
) -> None:
    """
    Test the task configuration by validating the config file and attempting to build task images.

    Optionally starts the task environment after testing.
    """

    if verbose:
        log.setLevel(logging.DEBUG)

    config = "task.yaml"
    if not test_for_file(config):
        config = "task.yml"
        if not test_for_file(config):
            log.error("No task configuration file found (task.yaml or task.yml)")
            log.error("Are you sure you are in the task directory?")
            return

    task = deserialise_task(Path(config).read_text())
    log.info(f"Loaded task: {task.name}")

    log.info("Testing Docker images for all pods...")
    client = docker.from_env()
    for pod in task.pods:
        with Status(f"Checking image for pod '{pod.name}'...", spinner="dots") as status:
            image_tag = f"{pod.image.image_name}:{task.version}"
            if not build:
                # See if we can find the image locally
                try:
                    client.images.get(image_tag)
                    status.update(f"Image '{image_tag}' found locally.")
                    log.info(f"Image '{image_tag}' found locally for pod '{pod.name}'.")
                    continue
                except docker.errors.ImageNotFound:
                    log.info(f"Image '{image_tag}' not found locally for pod '{pod.name}', building...")
            # Build the image
            status.update(f"Building image '{image_tag}'...")
            log.info(f"Building image '{image_tag}' for pod '{pod.name}'...")
            build_args = {arg.name: arg.value for arg in (pod.image.build_args or [])}
            try:
                try_build_image(image_tag, pod.image.build_context, build_args, verbose)
            except Exception as e:
                log.error(f"Failed to build image '{image_tag}' for pod '{pod.name}': {e}")
                return

    # Attempt to set up the entire task environment
    if up:
        log.info("Setting up the task environment...")
        containers = create_env(task)
        log_docker_logs(
            containers,
            lambda: cleanup_env(
                task.name, [pod.name for pod in task.pods], [net.name for net in task.networks]
            ),
        )
    log.info("Task test completed.")


def init(
    name: Annotated[str, typer.Argument(help="Name of the task.")],
    path: Annotated[Path, typer.Argument(help="Path to the task.", show_default=False)] = CWD,
) -> None:
    """
    Initialize a new task directory with a template configuration and an example image.

    Creates the following structure:\n
    <out_dir>/\n
    ├─task.yaml\n
    └─hello-world/\n
    ....└──Dockerfile\n
    """

    log.info(f"Initializing new task '{name}' at '{path}'...")
    out_dir = path / normalise_task_name(name)
    if out_dir.exists():
        log.error(f"Directory '{out_dir}' already exists.")
        return
    out_dir.mkdir(parents=True)
    log.info(f"Writing task template to '{out_dir}'...")
    write_task_yaml(out_dir, name)
    log.info(f"Copying example images to '{out_dir}'...")
    copy_example_images(out_dir)


def useless_gradient_function(step: int) -> str:
    # Calculate hue based on step
    hue = (step * 3 % 360) / 360.0
    # Convert hue to RGB
    red, green, blue = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
    # Output as ANSI
    return f"\x1b[38;2;{int(red * 255)};{int(green * 255)};{int(blue * 255)}m"


# Load bearing function, do not remove :3
def epic() -> None:
    """
    A very important function.
    """

    step = 0
    while True:
        print(f"\r{useless_gradient_function(step)}Epic function executed successfully.\x1b[0m", end="")
        step += 1
        time.sleep(0.1)
