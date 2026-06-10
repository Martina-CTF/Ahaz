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

# Shut up MongoDB driver logging
logging.getLogger("pymongo").setLevel(logging.WARNING)

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


async def init_indexes(collections: Collections) -> None:
    # Certificates
    # 1. Every single serial number MUST be unique
    await collections.certificates.create_index("serial_number", unique=True)
    # 2. Keep the latest cert for each common name on hand.
    await collections.certificates.create_index([("common_name", 1), ("valid_until", -1)])

    # Teams
    await collections.teams.create_index("team_id", unique=True)

    # Task Deployments
    await collections.task_deployments.create_index([("team_id", 1), ("task_name", 1)], unique=True)

    # Task Definitions
    # 1. Task defs should be immutable, thus, there cannot be more than one task def
    # with the same name and version. If we want to change sth, that's a new version.
    await collections.task_definitions.create_index([("name", 1), ("version", 1)], unique=True)
    # TODO: improve the index to better be able to query for latest version; semver makes it a bit fucky


async def init_db():
    context = await get_context()
    await init_indexes(context.collections)
    await close_context()


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
