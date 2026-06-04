import datetime
import logging
from os import getenv

from ahaz_common.task import PodInformation
from db.collections import get_context
from db.models import TaskDefinition

K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()


def getUTCasStr() -> str:
    return str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))


async def get_challenges_from_db() -> list[str]:
    database = await get_context()

    return await database.collections.task_definitions.distinct("name")


async def get_task_definition(name: str) -> TaskDefinition:
    database = await get_context()

    task: TaskDefinition | None = await database.collections.task_definitions.find_one({"name": name})

    if task is None:
        raise ValueError("challenge not found in db")

    return task
