import asyncio
import logging
from dataclasses import dataclass
from os import getenv

from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from ..util.misc import str_to_bool
from .models.certificate import CertificateDoc
from .models.deployment import TaskDeploymentDoc
from .models.task import TaskDoc
from .models.team import TeamDoc

DB_IP = getenv("DB_IP", None)
if DB_IP is None:
    raise ValueError("DB_IP environment variable is not set")
DB_DBNAME = getenv("DB_DBNAME", "ahaz")
DB_USERNAME = getenv("DB_USERNAME", "mongodb")
DB_PASSWORD = getenv("DB_PASSWORD", "I_LOVE_NOSQL!!!")

DB_TLS_ENABLED = str_to_bool(getenv("DB_TLS_ENABLED", "false"))
DB_TLS_CA_FILE = getenv("DB_TLS_CA_FILE", None)
DB_TLS_CLIENT_CERT = getenv("DB_TLS_CLIENT_CERT", None)
DB_TLS_INSECURE = getenv("DB_TLS_INSECURE", None)
if DB_TLS_INSECURE is not None:
    DB_TLS_INSECURE = str_to_bool(DB_TLS_INSECURE)

if DB_TLS_ENABLED and (DB_TLS_CA_FILE is None and DB_TLS_INSECURE is False):
    raise ValueError("DB_TLS_ENABLED is true, but no CA file is provided and DB_TLS_INSECURE is false")

# Shut up MongoDB driver logging
logging.getLogger("pymongo").setLevel(logging.WARNING)

logger = logging.getLogger()


@dataclass(slots=True)
class Collections:
    certificates: AsyncCollection[CertificateDoc]
    task_definitions: AsyncCollection[TaskDoc]
    teams: AsyncCollection[TeamDoc]
    task_deployments: AsyncCollection[TaskDeploymentDoc]


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
    # 1. There cannot be more than one team with the same team id
    await collections.teams.create_index("team_id", unique=True)

    # Task Deployments
    # 1. There cannot be more than one deployment of the same task for the same team at the same time
    await collections.task_deployments.create_index([("team_id", 1), ("task_name", 1)], unique=True)

    # Task Definitions
    # 1. Task defs should be immutable, thus, there cannot be more than one task def
    # with the same name and version. If we want to change sth, that's a new version.
    # Considering that version_serialized is derived from version, this indirectly enforces on version as well
    await collections.task_definitions.create_index([("name", 1), ("version_serialized", -1)], unique=True)


async def init_db():
    context = await get_context()
    await init_indexes(context.collections)
    await close_context()


async def get_context() -> MongoContext:
    loop = asyncio.get_running_loop()

    if loop not in contexts:
        tlsKwargs = {}
        if DB_TLS_ENABLED:
            tlsKwargs = {
                "tlsCAFile": DB_TLS_CA_FILE,
                "tlsCertificateKeyFile": DB_TLS_CLIENT_CERT,
                "tlsAllowInvalidCertificates": DB_TLS_INSECURE,
            }

        client = AsyncMongoClient(
            host=DB_IP,
            username=DB_USERNAME,
            password=DB_PASSWORD,
            tls=DB_TLS_ENABLED,
            **tlsKwargs,
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
            ),
        )

    return contexts[loop]


async def close_context():
    loop = asyncio.get_running_loop()

    if loop in contexts:
        await contexts[loop].client.close()
        del contexts[loop]
