import logging
from os import getenv

from ahaz_common.task import Task

from .collections import get_context
from .models.certificate import Certificate, CertificateDoc, cert_to_doc, doc_to_cert, extract_public_cert
from .models.task import TaskDefinitionDoc, task_to_task_doc
from .models.team import Team, TeamDoc

K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()


async def list_challenges() -> list[str]:
    database = await get_context()

    return await database.collections.task_definitions.distinct("name")


async def set_task_definition(task: Task) -> None:
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


async def set_range(team: Team) -> None:
    database = await get_context()

    team_doc = TeamDoc(team_id=team.team_id, port=team.port)

    await database.collections.teams.update_one(
        {"team_id": team.team_id},
        {"$set": team_doc},
        upsert=True,
    )


async def get_range(team_id: str) -> Team:
    database = await get_context()

    team_range: TeamDoc | None = await database.collections.teams.find_one({"team_id": team_id})

    if team_range is None:
        raise ValueError("range not found for team")

    return Team.model_validate(team_range)


async def set_certificate(cert: Certificate) -> None:
    database = await get_context()

    cert_doc = cert_to_doc(cert)

    await database.collections.certificates.insert_one(cert_doc)


async def get_certificate(serial_number: int) -> Certificate:
    database = await get_context()

    cert_doc: CertificateDoc | None = await database.collections.certificates.find_one(
        {"serial_number": serial_number}
    )

    if cert_doc is None:
        raise ValueError("certificate not found in db")

    return doc_to_cert(cert_doc)


async def get_certificate_by_common_name(common_name: str) -> Certificate:
    database = await get_context()

    # Find newest certificate with the given common name
    cert_doc: CertificateDoc | None = await database.collections.certificates.find_one(
        {"common_name": common_name}, sort=[("valid_until", -1)]
    )

    if cert_doc is None:
        raise ValueError("certificate not found in db")

    return doc_to_cert(cert_doc)


async def get_only_certificate_by_common_name(common_name: str) -> str:
    database = await get_context()

    certificate_bytes = await database.collections.certificates.find_one(
        {"common_name": common_name}, sort=[("valid_until", -1)], projection={"cert": 1, "_id": 0}
    )

    if certificate_bytes is None:
        raise ValueError("certificate not found in db")

    return extract_public_cert(certificate_bytes)


# TODO: dumbass zone, remove when possible
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
