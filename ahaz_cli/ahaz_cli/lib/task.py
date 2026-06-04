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
    dictionary = task.model_dump()

    return yaml.safe_dump(dictionary, sort_keys=False, default_flow_style=False)


def normalise_task_name(name: str) -> str:
    # Make the task lowercase, replace spaces with hyphens, and remove special characters
    return name.lower().replace(" ", "-").replace(r"([^a-z0-9-])", "")
