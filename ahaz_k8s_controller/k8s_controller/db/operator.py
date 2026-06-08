import logging
from os import getenv

from ahaz_common.task import Task
from db.collections import get_context
from db.models.task import TaskDefinitionDoc, task_to_task_doc
from db.models.team import Team, TeamDoc

K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()


async def list_challenges() -> list[str]:
    database = await get_context()

    return await database.collections.task_definitions.distinct("name")


async def push_task_definition(task: Task) -> None:
    database = await get_context()

    task_doc = task_to_task_doc(task)

    await database.collections.task_definitions.update_one(
        {"name": task_doc["name"]},
        {"$set": task_doc},
        upsert=True,
    )


async def get_task_definition(name: str) -> Task:
    database = await get_context()

    task: TaskDefinitionDoc | None = await database.collections.task_definitions.find_one({"name": name})

    if task is None:
        raise ValueError("challenge not found in db")

    return Task.model_validate(task)


async def get_range(team_id: str) -> Team:
    database = await get_context()

    team_range: TeamDoc | None = await database.collections.teams.find_one({"team_id": team_id})

    if team_range is None:
        raise ValueError("range not found for team")

    return Team.model_validate(team_range)


async def set_registration_progress_team(team_id: str, user_id: str, progress: int) -> None:
    database = await get_context()

    await database.collections.register_progress.update_one(
        {"team_id": team_id, "user_id": user_id},
        {"$set": {"progress": progress}},
        upsert=True,
    )


async def get_registration_progress_team(team_id: str, user_id: str) -> int | None:
    database = await get_context()

    progress_doc = await database.collections.register_progress.find_one(
        {"team_id": team_id, "user_id": user_id}
    )

    if progress_doc is None:
        return None

    return progress_doc["progress"]


async def get_registration_progress_team_any(team_id: str) -> int | None:
    database = await get_context()

    doc = await database.collections.register_progress.find_one({"team_id": team_id}, sort=[("progress", -1)])

    return doc["progress"] if doc else None
