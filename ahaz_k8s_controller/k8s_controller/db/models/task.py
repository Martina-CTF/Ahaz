from typing import Optional, TypedDict

from ahaz_common.task import Task

# Defines all the data we might want to store in a DB about a task definition.
# Subset of Task object from common.


class NetworkInformationDoc(TypedDict):
    name: str
    access: list[str]


class EnvironmentInformationDoc(TypedDict):
    name: str
    value: str


class LimitInformationDoc(TypedDict):
    ram: str
    cpu: str


class ImageInformationDoc(TypedDict):
    name: str
    # TODO: define registry and what other nonsense


class PodInformationDoc(TypedDict):
    name: str
    visible: bool
    image: ImageInformationDoc
    limits: LimitInformationDoc
    networks: list[str]
    env: list[EnvironmentInformationDoc]


class TaskInformationDoc(TypedDict):
    name: str
    flag: Optional[str]


class TaskDefinitionDoc(TypedDict):
    name: str
    api_version: Optional[str]
    version: Optional[str]

    info: Optional[TaskInformationDoc]
    pods: list[PodInformationDoc]
    networks: list[NetworkInformationDoc]


# TODO: Maybe a better way to drop unnecessary fields, idk.
def task_to_task_doc(task: Task) -> TaskDefinitionDoc:
    return TaskDefinitionDoc(
        name=task.name,
        api_version=task.api_version,
        version=task.version,
        info=TaskInformationDoc(
            name=task.info.name,
            flag=task.info.flag,
        )
        if task.info
        else None,
        pods=[
            PodInformationDoc(
                name=pod.name,
                visible=pod.visible,
                image=ImageInformationDoc(name=pod.image.name),
                limits=LimitInformationDoc(ram=pod.limits.ram, cpu=pod.limits.cpu),
                networks=pod.networks,
                env=[EnvironmentInformationDoc(name=e.name, value=e.value) for e in pod.env],
            )
            for pod in task.pods
        ],
        networks=[
            NetworkInformationDoc(
                name=net.name,
                access=[a.value for a in net.access],
            )
            for net in task.networks
        ],
    )
