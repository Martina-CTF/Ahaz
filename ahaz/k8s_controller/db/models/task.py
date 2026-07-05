from typing import Optional, TypedDict

from ahaz_common.task import NetworkInformation, PodInformation, Task

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
    ephemeral_storage: str


class ImageInformationDoc(TypedDict):
    name: str
    registry: Optional[str]


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


class TaskDoc(TypedDict):
    name: str
    api_version: Optional[str]
    version: Optional[str]
    version_serialized: list[int]

    info: Optional[TaskInformationDoc]
    pods: list[PodInformationDoc]
    networks: list[NetworkInformationDoc]


def pod_to_pod_doc(pod: PodInformation) -> PodInformationDoc:
    return PodInformationDoc(
        name=pod.name,
        visible=pod.visible,
        image=ImageInformationDoc(name=pod.image.name, registry=pod.image.registry),
        limits=LimitInformationDoc(
            ram=pod.limits.ram, cpu=pod.limits.cpu, ephemeral_storage=pod.limits.ephemeral_storage
        ),
        networks=pod.networks,
        env=[EnvironmentInformationDoc(name=e.name, value=e.value) for e in pod.env],
    )


def network_to_network_doc(net: NetworkInformation) -> NetworkInformationDoc:
    return NetworkInformationDoc(
        name=net.name,
        access=[a.value for a in net.access],
    )


def task_to_task_doc(task: Task) -> TaskDoc:
    return TaskDoc(
        name=task.name,
        api_version=task.api_version,
        version=task.version,
        # This makes sorting/querying by version easier/faster, it does not matter anywhere but the DB layer.
        version_serialized=[int(x) for x in task.version.split(".")] if task.version else [],
        info=(
            TaskInformationDoc(
                name=task.info.name,
                flag=task.info.flag,
            )
            if task.info
            else None
        ),
        pods=[pod_to_pod_doc(pod) for pod in task.pods],
        networks=[network_to_network_doc(net) for net in task.networks],
    )
