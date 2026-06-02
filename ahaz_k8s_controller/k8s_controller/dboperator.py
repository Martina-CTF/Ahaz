import datetime
import json
import logging
from contextlib import asynccontextmanager
from os import getenv
from typing import Any, AsyncGenerator, TypedDict

from psycopg.connection_async import AsyncConnection
from psycopg.cursor_async import AsyncCursor
from psycopg.errors import DatabaseError, InterfaceError
from psycopg.types.json import Jsonb
from psycopg_pool.pool_async import AsyncConnectionPool
from schemas import DBTeam, DBUser, NetworkDefinition, PodDefinition, Task

logger = logging.getLogger()

# TODO: use cursor row factories

DB_IP = getenv("DB_IP", "10.33.0.3")
DB_DBNAME = getenv("DB_DBNAME", "ahaz")
DB_USERNAME = getenv("DB_USERNAME", "dbeaver")
K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")
DB_PASSWORD = getenv("DB_PASSWORD")
if DB_PASSWORD is None:
    DB_PASSWORD_FILE = getenv("DB_PASSWORD_FILE", "/run/secrets/db_password")
    with open(DB_PASSWORD_FILE, "r") as f:
        DB_PASSWORD = f.read()
else:
    logger.warning("The DB_PASSWORD environment variable is deprecated for Ahaz")

pool = None


@asynccontextmanager
async def get_connection() -> AsyncGenerator[tuple[AsyncConnection, AsyncCursor[Any]], None]:
    DB_CONNECTION_TIMEOUT = 1

    global pool
    if pool is None:
        logger.debug("Initializing connection pool")
        pool = AsyncConnectionPool(
            f"host={DB_IP} dbname={DB_DBNAME} user={DB_USERNAME} password={DB_PASSWORD}",
            # TODO: Implement core-counted pool size
            # See: https://github.com/brettwooldridge/HikariCP/wiki/About-Pool-Sizing
            min_size=4,
            max_size=16,
            max_idle=60,  # One minute
            max_waiting=4,  # Allow 4 waiting for a connection before creating a new connection
            open=False,
        )
        await pool.open()
    try:
        async with pool.connection(timeout=DB_CONNECTION_TIMEOUT) as conn, conn.cursor() as cur:
            yield conn, cur
    except TimeoutError:
        logger.error("Timed out while waiting for a database connection")
        raise
    except InterfaceError as e:
        logger.error("Database connection error: %s", e)
        raise
    except DatabaseError as e:
        logger.error("Database error: %s", e)
        raise
    except Exception as e:  # noqa: E722
        logger.error("Error occurred while fetching connection: %s", e)
        raise


def getUTCasStr() -> str:
    return str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))


async def get_tasks() -> list[Task]:
    async with get_connection() as (conn, cursor):
        await cursor.execute("SELECT name, pods, networks FROM tasks")
        rows = await cursor.fetchall()
    return [Task(name=str(row[0]), pods=row[1], networks=row[2]) for row in rows]


async def get_pods(task: str) -> list[PodDefinition]:
    async with get_connection() as (conn, cursor):
        await cursor.execute("SELECT pods FROM tasks WHERE name = %s", (task,))
        rows = await cursor.fetchall()
    return [PodDefinition(**row[0]) for row in rows]


async def get_networks(task: str) -> list[NetworkDefinition]:
    async with get_connection() as (conn, cursor):
        await cursor.execute("SELECT networks FROM tasks WHERE name = %s", (task,))
        rows = await cursor.fetchall()
    return [NetworkDefinition(**row[0]) for row in rows]


async def upsert_task(task: Task) -> bool:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            """
            INSERT INTO tasks (name, pods, networks) VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET pods = EXCLUDED.pods, networks = EXCLUDED.networks
            RETURNING xmax = 0
            """,
            (
                task.name,
                Jsonb([pod.model_dump() for pod in task.pods]),
                Jsonb([network.model_dump() for network in task.networks]),
            ),
        )
        fresh = await cursor.fetchone()  # True if the row was inserted, False if it was updated
        if fresh is not None:
            fresh = fresh[0]
        else:
            fresh = False  # Should never happen, but just in case
        await conn.commit()
        return fresh  # pyright: ignore[reportReturnType]


async def delete_task(task: str) -> bool:
    async with get_connection() as (conn, cursor):
        await cursor.execute("DELETE FROM tasks WHERE name = %s", (task,))
        deleted = cursor.rowcount > 0
        await conn.commit()
    return deleted


async def insert_team(team: str, namespace: str, min_vpn_port: int) -> DBTeam:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            """
            -- Find the next available VPN port for the team and insert the team with that port
            WITH usable_port AS (
                SELECT generate_series(%s::int, 65535) AS port -- (min_port, max_port)
                EXCEPT
                SELECT vpn_port FROM teams
                ORDER BY port
                LIMIT 1
            )
            INSERT INTO teams (id, namespace, vpn_port) VALUES (
                %s, %s,
                (SELECT port FROM usable_port LIMIT 1)
            )
            RETURNING vpn_port
            """,
            (min_vpn_port, team, namespace),
        )
        result = await cursor.fetchone()
        if result is None:
            await conn.rollback()
            raise Exception("No available VPN ports")
        vpn_port = result[0]
        await conn.commit()

        return DBTeam(name=team, namespace=namespace, vpn_port=vpn_port, vpn_config=None, users=[])


async def get_team_with_users(team: str) -> DBTeam | None:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            """
            SELECT t.id, t.namespace, t.vpn_port, t.vpn_config AS team_vpn_config, u.id, u.vpn_config
            FROM teams t
            LEFT JOIN users u ON t.id = u.team_id
            WHERE t.id = %s
            """,
            (team,),
        )
        rows = await cursor.fetchall()

    if not rows:
        return None

    team_id, namespace, vpn_port, team_vpn_config, _, _ = rows[0]
    users = []
    for row in rows:
        user_id, user_vpn_config = row[4], row[5]
        if user_id is not None:  # Only add users if they exist (LEFT JOIN can result in NULLs for users)
            users.append(DBUser(id=user_id, vpn_config=user_vpn_config))

    return DBTeam(
        name=team_id, namespace=namespace, vpn_port=vpn_port, vpn_config=team_vpn_config, users=users
    )


async def get_all_teams_with_users() -> list[DBTeam]:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            """
            SELECT t.id, t.namespace, t.vpn_port, t.vpn_config AS team_vpn_config, u.id, u.vpn_config
            FROM teams t
            LEFT JOIN users u ON t.id = u.team_id
            """,
        )
        rows = await cursor.fetchall()

    teams_dict: dict[str, DBTeam] = {}
    for row in rows:
        team_id, namespace, vpn_port, team_vpn_config, user_id, user_vpn_config = row
        if team_id not in teams_dict:
            teams_dict[team_id] = DBTeam(
                name=team_id,
                namespace=namespace,
                vpn_port=vpn_port,
                vpn_config=team_vpn_config,
                users=[],
            )
        if user_id is not None:  # Only add users if they exist (LEFT JOIN can result in NULLs for users)
            teams_dict[team_id].users.append(DBUser(id=user_id, vpn_config=user_vpn_config))

    return list(teams_dict.values())


async def delete_team(team: str) -> bool:
    async with get_connection() as (conn, cursor):
        await cursor.execute("DELETE FROM teams WHERE id = %s", (team,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_users(team: str) -> list[DBUser] | None:
    async with get_connection() as (conn, cursor):
        await cursor.execute("SELECT 1 FROM teams WHERE id = %s", (team,))
        if not await cursor.fetchone():
            return None

        await cursor.execute("SELECT id, vpn_config FROM users WHERE team_id = %s", (team,))
        rows = await cursor.fetchall()
    return [DBUser(id=row[0], vpn_config=row[1]) for row in rows]


async def get_user(team: str, user_id: str) -> DBUser | None:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            "SELECT id, vpn_config FROM users WHERE team_id = %s AND id = %s", (team, user_id)
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return DBUser(id=row[0], vpn_config=row[1])


async def insert_user(team: str, user_id: str) -> DBUser:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            "INSERT INTO users (id, team_id) VALUES (%s, %s) RETURNING id, vpn_config",
            (user_id, team),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.rollback()
            raise Exception("Failed to insert user")
        await conn.commit()
    return DBUser(id=row[0], vpn_config=row[1])


async def delete_user(team: str, user_id: str) -> bool:
    async with get_connection() as (conn, cursor):
        await cursor.execute("DELETE FROM users WHERE team_id = %s AND id = %s", (team, user_id))
        await conn.commit()
        return cursor.rowcount > 0


# --- LEGACY CODE BELOW, TO BE REMOVED ---


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


async def insert_user_vpn_config(user: str, config: str) -> None:
    # FIXME: what the fuck??
    config = str(config).replace("\\n", "\n")
    config = config.replace(
        "<key>", "route-nopull\nroute " + parse_ip_range(K8S_IP_RANGE) + "\n\n<key>"
    )  # add IP route to the config
    config = config.replace("redirect-gateway def1", "")  # remove the rule that replaces all routes with VPN
    config = config + "\ncomp-lzo yes\nallow-compression yes"

    async with get_connection() as (conn, cursor):
        await cursor.execute("UPDATE users SET vpn_config = %s WHERE id = %s", (config, user))
        await conn.commit()


async def get_registration_progress_team(teamname: str) -> int:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            "SELECT state FROM register_status WHERE name=%s ORDER BY state DESC", (teamname,)
        )
        rows = await cursor.fetchall()

    if len(rows) == 0 or len(rows[0]) == 0:
        return -999
    return int(rows[0][0])


async def get_registration_progress_user(teamname: str, username: str) -> str:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            "SELECT state FROM register_status WHERE name=%s and username=%s ORDER BY state DESC",
            (teamname, username),
        )
        rows = await cursor.fetchall()
    if len(rows) == 0 or len(rows[0]) == 0:
        return "null"
    return rows[0][0]


async def set_registration_progress_team(teamname: str, username: str, status: int) -> None:
    async with get_connection() as (conn, cursor):
        await cursor.execute(
            "INSERT INTO register_status (name, username, state, timest) VALUES (%s, %s, %s, %s)",
            (teamname, username, status, getUTCasStr()),
        )
        await conn.commit()
