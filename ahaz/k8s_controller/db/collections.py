import asyncio
import logging
from dataclasses import dataclass
from os import getenv

from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from .models.certificate import CertificateDoc
from .models.deployment import TaskDeploymentDoc
from .models.registerprocess import RegisterProgressDoc
from .models.task import TaskDefinitionDoc
from .models.team import TeamDoc

DB_IP = getenv("DB_IP", "10.33.0.3")
DB_DBNAME = getenv("DB_DBNAME", "ahaz")
DB_USERNAME = getenv("DB_USERNAME", "dbeaver")
DB_PASSWORD = getenv("DB_PASSWORD", "dbeaver")

logger = logging.getLogger()


@dataclass(slots=True)
class Collections:
    certificates: AsyncCollection[CertificateDoc]
    task_definitions: AsyncCollection[TaskDefinitionDoc]
    teams: AsyncCollection[TeamDoc]
    task_deployments: AsyncCollection[TaskDeploymentDoc]
    register_progress: AsyncCollection[RegisterProgressDoc]


@dataclass(slots=True)
class MongoContext:
    client: AsyncMongoClient
    db: AsyncDatabase
    collections: Collections


contexts: dict[asyncio.AbstractEventLoop, MongoContext] = {}


async def get_context() -> MongoContext:
    loop = asyncio.get_running_loop()

    if loop not in contexts:
        client = AsyncMongoClient(
            host=DB_IP,
            username=DB_USERNAME,
            password=DB_PASSWORD,
            tls=False,  # TODO: Add TLS support in env vars
        )
        db = client[DB_DBNAME]

        contexts[loop] = MongoContext(
            client=client,
            db=db,
            collections=Collections(
                certificates=db["certificates"],
                task_definitions=db["task_definitions"],
                teams=db["teams"],
                task_deployments=db["task_deployments"],
                register_progress=db["register_progress"],
            ),
        )

    return contexts[loop]


async def close_context():
    loop = asyncio.get_running_loop()

    if loop in contexts:
        await contexts[loop].client.close()
        del contexts[loop]
