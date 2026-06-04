import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from os import getenv
from typing import AsyncGenerator

from db.collections import get_context
from db.models import Pod, RegisterStatus, TaskDefinition, Team, VPNMap, VPNStorage
from pydantic import BaseModel
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()


def getUTCasStr() -> str:
    return str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))


async def get_challenges_from_db() -> list[str]:
    database = await get_context()

    return await database.collections.task_definitions.distinct("name")


async def get_pods(name: str) -> list[str]:
    database = await get_context()

    task: TaskDefinition | None = await database.collections.task_definitions.find_one({"name": name})

    if task is None:
        raise ValueError("challenge not found in db")

    return [pod.name for pod in task["definition"].pods]


# TODO: This fucking sucks, but I am changing the schema anyways so womp womp
class EnvVarOut(BaseModel):
    name: str
    value: str


async def get_env_vars(task_name: str, pod_name: str) -> dict[str, str]:
    database = await get_context()

    docs: TaskDefinition | None = await database.collections.task_definitions.find_one({"name": task_name})

    if docs is None:
        raise ValueError("task not found in db")

    pod: Pod | None = next((pod for pod in docs["definition"].pods if pod.name == pod_name), None)

    if pod is None:
        raise ValueError("pod not found in task")

    return {env_var.name: env_var.value for env_var in pod.env}


async def get_task_networks(task_name: str) -> list[str]:
    database = await get_context()

    docs: TaskDefinition | None = await database.collections.task_definitions.find_one({"name": task_name})

    if docs is None:
        raise ValueError("task not found in db")

    return [net.name for net in docs["definition"].networks]


async def get_unique_networks(challengename: str) -> list[str]:
    database = await get_context()
    return await database.collections.net_rules.find({"name": challengename}).distinct("netname")


async def get_pods_in_network(challengename: str, netname: str) -> list[str]:
    database = await get_context()

    return await database.collections.net_rules.find({"name": challengename, "netname": netname}).distinct(
        "k8s_name"
    )


async def get_challenge_from_k8s_name(k8s_name: str) -> str | None:
    database = await get_context()
    pod: Pod | None = await database.collections.pods.find_one({"k8s_name": k8s_name})

    return pod["name"] if pod else None


async def insert_team_into_db(teamname: str) -> None:
    database = await get_context()
    if await database.collections.teams.find_one({"name": teamname}):
        raise ValueError("team with that name already exists in db")
    await database.collections.teams.insert_one({"name": teamname})


async def insert_vpn_port_into_db(teamname: str, port: int) -> str | None:
    if get_team_port(teamname) is not None:
        return "team already has port allocated to it"
    if get_port_team(port) is not None:
        return "port " + str(port) + " is already allocated"

    database = await get_context()
    teamid = await get_team_id(teamname)

    if teamid is None:
        return "team not found in db"

    await database.collections.vpnmap.insert_one(VPNMap(teamID=teamid, port=port))

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
    database = await get_context()

    config = str(config).replace("\\n", "\n")
    config = config.replace(
        "<key>", "route-nopull\nroute " + parse_ip_range(K8S_IP_RANGE) + "\n\n<key>"
    )  # add IP route to the config
    config = config.replace("redirect-gateway def1", "")  # remove the rule that replaces all routes with VPN
    config = config + "\ncomp-lzo yes\nallow-compression yes"

    teamid = await get_team_id(teamname)

    if teamid is None:
        raise ValueError("team not found in db")

    await database.collections.vpnstorage.insert_one(
        VPNStorage(teamID=teamid, username=username, config=config)
    )


async def get_team_id(teamname: str) -> str | None:
    database = await get_context()

    team = await database.collections.teams.find_one({"name": teamname})

    return team["name"] if team else None


async def get_team_port(teamname: str) -> int | None:
    teamID = await get_team_id(teamname)
    database = await get_context()

    result: VPNMap | None = await database.collections.vpnmap.find_one({"teamID": teamID})
    if result is None:
        return None
    return result["port"]


async def get_port_team(port: int) -> str | None:
    database = await get_context()
    result: VPNMap | None = await database.collections.vpnmap.find_one({"port": port})
    if result is None:
        return None
    return result["teamID"]


async def get_user_vpn_config(teamname: str, username: str) -> str | None:
    teamID = await get_team_id(teamname)
    database = await get_context()

    result: VPNStorage | None = await database.collections.vpnstorage.find_one(
        {"teamID": teamID, "username": username}
    )
    if result is None:
        return None
    return result["config"]


async def get_last_port() -> int:
    database = await get_context()

    result: VPNMap | None = await database.collections.vpnmap.find_one(sort=[("port", -1)])
    if result is None:
        return 0
    return int(result["port"])


async def delete_team_and_vpn(teamname: str) -> None:
    teamID = await get_team_id(teamname)
    database = await get_context()

    await database.collections.register_status.delete_many({"name": teamname})
    await database.collections.vpnmap.delete_many({"teamID": teamID})
    await database.collections.vpnstorage.delete_many({"teamID": teamID})
    await database.collections.teams.delete_many({"teamID": teamID})


async def get_registration_progress_team(teamname: str) -> int:
    database = await get_context()
    result: RegisterStatus | None = await database.collections.register_status.find_one(
        {"name": teamname}, sort=[("state", -1)]
    )

    if result is None:
        return -999

    logger.error(f"Registration progress for team {teamname}: {result}")
    return result["state"]


async def get_registration_progress_user(teamname: str, username: str) -> int:
    database = await get_context()
    result: RegisterStatus | None = await database.collections.register_status.find_one(
        {"name": teamname, "user": username}, sort=[("state", -1)]
    )

    if result is None:
        return -999

    logger.error(f"Registration progress for team {teamname} and user {username}: {result}")
    return result["state"]


async def set_registration_progress_team(teamname: str, username: str, status: int) -> None:
    database = await get_context()
    await database.collections.register_status.insert_one(
        {
            "name": teamname,
            "user": username,
            "state": status,
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
        }
    )
