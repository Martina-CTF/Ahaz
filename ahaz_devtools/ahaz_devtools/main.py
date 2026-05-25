import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import rich.logging
import watchdog.events
import watchdog.observers

from .lib.docker import (
    build_and_push_ahaz_image,
    create_local_registry,
    delete_local_registry,
    docker_is_available,
)
from .lib.kubernetes import (
    create_kind_cluster,
    delete_kind_cluster,
    forward_ahaz_port,
    install_ahaz,
    install_cilium,
    install_kyverno,
    is_helm_installed,
    is_kind_installed,
    restart_ahaz,
    setup_local_registry_in_kind,
)

logger = logging.getLogger()
logger.addHandler(rich.logging.RichHandler(markup=True))
logger.setLevel(logging.INFO)

# define and parse command line arguments
argparser = argparse.ArgumentParser(description="Initialize the Kubernetes cluster and deploy Ahaz.")
argparser.add_argument(
    "-c",
    "--chart",
    help="Path to the Helm chart to deploy Ahaz with",
    default="oci://ghcr.io/martina-ctf/helm-charts/ahaz",
)
argparser.add_argument(
    "-p", "--ahaz-port", help="Local port to forward Ahaz API to (default: 8080)", default=8080, type=int
)
argparser.add_argument(
    "--registry-port", help="Port for the local Docker registry (default: 6767)", default=6767, type=int
)
args = argparser.parse_args()


def init_cluster():
    if not is_kind_installed():
        logger.error("Kind is not installed. Please install Kind to proceed.")
        sys.exit(1)

    if not is_helm_installed():
        logger.error("Helm is not installed. Please install Helm to proceed.")
        sys.exit(1)

    if not docker_is_available():
        logger.error("Docker is not available. Please ensure Docker is running and accessible to proceed.")
        sys.exit(1)

    create_kind_cluster()

    create_local_registry(args.registry_port)

    setup_local_registry_in_kind()

    install_cilium()

    install_kyverno()

    build_and_push_ahaz_image(args.registry_port)

    install_ahaz(args.chart)


def delete_cluster():
    delete_kind_cluster()

    delete_local_registry()


def build(forward=True):
    if not docker_is_available():
        logger.error("Docker is not available. Please ensure Docker is running and accessible to proceed.")
        sys.exit(1)

    build_and_push_ahaz_image(args.registry_port)
    restart_ahaz()
    if forward:
        logger.info(f"Forwarding Ahaz API to localhost:{args.ahaz_port}...")
        forward_ahaz_port(args.ahaz_port)


def watch_forward():
    logger.info(f"Forwarding Ahaz API to localhost:{args.ahaz_port}...")
    while True:
        forward_ahaz_port(args.ahaz_port)


# Watches root directory for changes and rebuilds and redeploys Ahaz on change
def watch():
    if not docker_is_available():
        logger.error("Docker is not available. Please ensure Docker is running and accessible to proceed.")
        sys.exit(1)

    root = Path(__file__).resolve().parent.parent.parent  # Get project root
    logger.info("Building and deploying Ahaz to cluster...")
    build(forward=False)
    logger.info(f"Watching {root} for changes to Ahaz source code...")

    # Forward Ahaz port in a separate thread so it doesn't block the file watcher
    threading.Thread(target=watch_forward, daemon=True).start()

    class ChangeHandler(watchdog.events.FileSystemEventHandler):
        def on_modified(self, event):
            if event.is_directory:
                return

            rawPath: bytes | str = os.fspath(event.src_path)

            # ensure rawPath is a string, decoding if necessary
            path: str = ""
            if isinstance(rawPath, (bytes, bytearray)):
                path = rawPath.decode(errors="ignore")
            else:
                assert isinstance(rawPath, str), "Expected rawPath to be a string after os.fspath"
                path = rawPath

            if path.endswith((".py", ".yaml", ".yml")) or "Dockerfile" in path.split(os.sep)[-1]:
                logger.info(f"Change detected in {event.src_path}, rebuilding and redeploying Ahaz...")
                build(forward=False)

    event_handler = ChangeHandler()
    observer = watchdog.observers.Observer()
    observer.schedule(event_handler, str(root), recursive=True)
    observer.start()

    signal.sigwait([signal.SIGINT, signal.SIGTERM])  # Keep the main thread alive

    logger.info("Shutting down...")

    observer.stop()

    exit_counter = 0

    THRESHOLD = 3
    while exit_counter < THRESHOLD:
        try:
            observer.join()
        except KeyboardInterrupt:
            exit_counter += 1
            if exit_counter < THRESHOLD:
                logger.info(
                    f"Received exit signal ({exit_counter}/{THRESHOLD})."
                    f" Press Ctrl+C {THRESHOLD - exit_counter} more times to force exit."
                )
            pass

    logger.info("Bye-bye!")
