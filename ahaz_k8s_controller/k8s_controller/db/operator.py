import logging
from os import getenv

from db.collections import get_context
from db.models import Range, TaskDefinition

K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()


async def list_challenges() -> list[str]:
    database = await get_context()

    return await database.collections.task_definitions.distinct("name")


async def get_task_definition(name: str) -> TaskDefinition:
    database = await get_context()

    task: TaskDefinition | None = await database.collections.task_definitions.find_one({"name": name})

    if task is None:
        raise ValueError("challenge not found in db")

    return task


async def get_range(team_id: str) -> Range:
    database = await get_context()

    team_range: Range | None = await database.collections.ranges.find_one({"team_id": team_id})

    if team_range is None:
        raise ValueError("range not found for team")

    return team_range


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
