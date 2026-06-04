import asyncio
import logging
from dataclasses import dataclass
from os import getenv

from db.models import Challenge, EnvVar, NetRule, Pod, RegisterStatus, Team, VPNMap, VPNStorage
from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

DB_IP = getenv("DB_IP", "10.33.0.3")
DB_DBNAME = getenv("DB_DBNAME", "ahaz")
DB_USERNAME = getenv("DB_USERNAME", "dbeaver")
DB_PASSWORD = getenv("DB_PASSWORD", "dbeaver")

logger = logging.getLogger()


@dataclass(slots=True)
class Collections:
    teams: AsyncCollection[Team]
    vpnmap: AsyncCollection[VPNMap]
    vpnstorage: AsyncCollection[VPNStorage]
    challenges: AsyncCollection[Challenge]
    pods: AsyncCollection[Pod]
    net_rules: AsyncCollection[NetRule]
    env_vars: AsyncCollection[EnvVar]
    register_status: AsyncCollection[RegisterStatus]


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
                teams=db["teams"],
                vpnmap=db["vpnmap"],
                vpnstorage=db["vpnstorage"],
                challenges=db["challenges"],
                pods=db["pods"],
                net_rules=db["net_rules"],
                env_vars=db["env_vars"],
                register_status=db["register_status"],
            ),
        )

    return contexts[loop]


async def close_context():
    loop = asyncio.get_running_loop()

    if loop in contexts:
        await contexts[loop].client.close()
        del contexts[loop]
