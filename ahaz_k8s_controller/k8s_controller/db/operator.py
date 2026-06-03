import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from os import getenv
from typing import AsyncGenerator

from db.models import Pod, RegisterStatus, Team, VPNMap, VPNStorage
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

DB_IP = getenv("DB_IP", "10.33.0.3")
DB_DBNAME = getenv("DB_DBNAME", "ahaz")
DB_USERNAME = getenv("DB_USERNAME", "dbeaver")
DB_PASSWORD = getenv("DB_PASSWORD", "dbeaver")
K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()

connection_map: dict[asyncio.AbstractEventLoop, AsyncMongoClient] = {}


async def get_database() -> AsyncDatabase:
    global connection_map

    loop = asyncio.get_event_loop()

    if loop not in connection_map:
        logger.debug("Initializing MongoDB connection")
        connection = AsyncMongoClient(
            host=DB_IP,
            username=DB_USERNAME,
            password=DB_PASSWORD,
            tls=False,  # TODO: Add TLS support in env vars
        )
        connection_map[loop] = connection
    else:
        logger.debug("Reusing existing MongoDB connection")
        connection = connection_map[loop]

    return connection[DB_DBNAME]


def getUTCasStr() -> str:
    return str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))


async def get_challenges_from_db() -> list[str]:
    database = await get_database()

    return await database["challenges"].distinct("name")


async def get_pods(name: str) -> list[tuple]:
    database = await get_database()

    return await database["pods"].find({"name": name}).to_list(length=None)


async def get_env_vars(k8s_name: str) -> list[dict]:
    database = await get_database()

    return await (
        await database["env_vars"].aggregate(
            [
                {"$match": {"k8s_name": k8s_name}},
                {
                    "$project": {
                        "_id": 0,
                        "name": {"$toUpper": "$env_var_name"},
                        "value": "$env_var_value",
                    }
                },
            ]
        )
    ).to_list(length=None)


async def get_k8s_name_networks(k8s_name: str) -> list[str]:
    database = await get_database()

    return await database["net_rules"].find({"k8s_name": k8s_name}).distinct("netname")


async def get_unique_networks(challengename: str) -> list[str]:
    database = await get_database()
    return await database["net_rules"].find({"name": challengename}).distinct("netname")


async def get_pods_in_network(challengename: str, netname: str) -> list[str]:
    database = await get_database()

    return await database["net_rules"].find({"name": challengename, "netname": netname}).distinct("k8s_name")


async def get_challenge_from_k8s_name(k8s_name: str) -> str | None:
    database = await get_database()
    pod: Pod | None = await database["pods"].find_one({"k8s_name": k8s_name})

    return pod.name if pod else None


async def insert_team_into_db(teamname: str) -> None:
    database = await get_database()
    if await database["teams"].find_one({"name": teamname}):
        raise ValueError("team with that name already exists in db")
    await database["teams"].insert_one({"name": teamname})


async def insert_vpn_port_into_db(teamname: str, port: int) -> str | None:
    if get_team_port(teamname) is not None:
        return "team already has port allocated to it"
    if get_port_team(port) is not None:
        return "port " + str(port) + " is already allocated"

    database = await get_database()
    if get_team_id(teamname) is None:
        return "team not found in db"
    await database["vpn_map"].insert_one({"teamID": teamname, "port": port})
    return None


# Mmmmm, cider...
def cidr_to_netmask(cidr: int) -> str:
    mask = (0xFFFFFFFF >> (32 - cidr)) << (32 - cidr)
    return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"


def ip_and_cidr_to_netmask(ip_cidr: str) -> str:
    ip, cidr = ip_cidr.split("/")
    cidr = int(cidr)
    netmask = cidr_to_netmask(cidr)
    return ip + " " + netmask


def parse_ip_range(ip_range: str) -> str:
    if ip_range.count("/") == 1:
        return ip_and_cidr_to_netmask(ip_range)
    elif ip_range.count(" ") == 1:
        return ip_range
    else:
        raise ValueError("Invalid IP range format")


async def insert_user_vpn_config(teamname: str, username: str, config: str) -> None:
    database = await get_database()

    config = str(config).replace("\\n", "\n")
    config = config.replace(
        "<key>", "route-nopull\nroute " + parse_ip_range(K8S_IP_RANGE) + "\n\n<key>"
    )  # add IP route to the config
    config = config.replace("redirect-gateway def1", "")  # remove the rule that replaces all routes with VPN
    config = config + "\ncomp-lzo yes\nallow-compression yes"

    teamid = get_team_id(teamname)
    await database["vpn_storage"].insert_one({"teamID": teamid, "username": username, "config": config})


async def get_team_id(teamname: str) -> str | None:
    database = await get_database()

    return await database["teams"].find_one({"name": teamname})


async def get_team_port(teamname: str) -> str | None:
    teamID = await get_team_id(teamname)
    database = await get_database()

    result: VPNMap | None = await database["vpn_map"].find_one({"teamID": teamID})
    if result is None:
        return None
    return result.port


async def get_port_team(port: int) -> str | None:
    database = await get_database()
    result: VPNMap | None = await database["vpn_map"].find_one({"port": port})
    if result is None:
        return None
    return result.teamID


async def get_user_vpn_config(teamname: str, username: str) -> str | None:
    teamID = await get_team_id(teamname)
    database = await get_database()

    result: VPNStorage | None = await database["vpn_storage"].find_one(
        {"teamID": teamID, "username": username}
    )
    if result is None:
        return None
    return result.config


async def get_last_port() -> int:
    database = await get_database()

    result: VPNMap | None = await database["vpn_map"].find_one(sort=[("port", -1)])
    if result is None:
        return 0
    return int(result.port)


async def delete_team_and_vpn(teamname: str) -> None:
    teamID = await get_team_id(teamname)
    database = await get_database()

    await database["register_status"].delete_many({"name": teamname})
    await database["vpn_map"].delete_many({"teamID": teamID})
    await database["vpn_storage"].delete_many({"teamID": teamID})
    await database["teams"].delete_many({"teamID": teamID})


async def get_registration_progress_team(teamname: str) -> int:
    database = await get_database()
    result: RegisterStatus | None = await database["register_status"].find_one(
        {"name": teamname}, sort=[("state", -1)]
    )

    if result is None:
        return -999

    logger.error(f"Registration progress for team {teamname}: {result}")
    return result["state"]


async def get_registration_progress_user(teamname: str, username: str) -> int:
    database = await get_database()
    result: RegisterStatus | None = await database["register_status"].find_one(
        {"name": teamname, "user": username}, sort=[("state", -1)]
    )

    if result is None:
        return -999

    logger.error(f"Registration progress for team {teamname} and user {username}: {result}")
    return result["state"]


async def set_registration_progress_team(teamname: str, username: str, status: int) -> None:
    database = await get_database()
    await database["register_status"].insert_one(
        {
            "name": teamname,
            "user": username,
            "state": status,
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
        }
    )
