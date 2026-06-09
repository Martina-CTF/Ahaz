import logging

import yaml

from ahaz_common import Task

log = logging.getLogger()


def deserialise_task(task_config: str) -> Task:
    try:
        config_dict = yaml.safe_load(task_config)
    except yaml.YAMLError as e:
        log.error(f"Error parsing YAML: {e}")
        raise ValueError("Invalid task configuration") from e

    try:
        return Task(**config_dict)
    except TypeError as e:
        log.error(f"Error constructing Task object: {e}")
        raise ValueError("Invalid task configuration structure") from e


# TODO: Figure out a better way to handle serialisation of nested objects
# TODO: Is this even necessary?
def serialise_task(task: Task) -> str:
    """
    Serialise a Task into YAML with a fixed field order matching the example:
    name, version, description, score, scoring_type, pods, networks, env_vars
    """

    # helper getters to be defensive about missing attributes
    def _image_dict(pod):
        img = getattr(pod, "image", None) or {}
        return {
            "image_name": getattr(img, "image_name", None),
            "build_context": getattr(img, "build_context", None),
        }

    def _testing_dict(pod):
        testing = getattr(pod, "testing", None)
        return {"exposed_ports": testing.exposed_ports if testing is not None else []}

    # Build top-level mapping in the desired order (insertion order is preserved)
    config_dict = {
        "name": task.name,
        "version": getattr(task, "version", "1.0.0"),
        "description": task.description,
        "score": task.score,
        "scoring_type": task.scoring_type,
        "pods": [
            {
                "name": pod.name,
                "image": _image_dict(pod),
                "limits_ram": getattr(pod, "limits_ram", None),
                "limits_cpu": getattr(pod, "limits_cpu", None),
                "visible_to_user": getattr(pod, "visible_to_user", None),
                # include build for convenience (matches example where build == build_context)
                "build": getattr(getattr(pod, "image", None), "build_context", None),
                "testing": _testing_dict(pod),
            }
            for pod in task.pods or []
        ],
        "networks": [{"name": net.name, "devices": list(net.devices)} for net in task.networks or []],
        "env_vars": [
            {
                # map original fields to the example-style names
                "pod_name": env.pod_name,
                "name": env.name,
                "value": env.value,
            }
            for env in task.env_vars or []
        ],
    }

    # Preserve insertion order in output
    return yaml.safe_dump(config_dict, sort_keys=False)


def normalise_task_name(name: str) -> str:
    # Make the task lowercase, replace spaces with hyphens, and remove special characters
    return name.lower().replace(" ", "-").replace(r"([^a-z0-9-])", "")
