import datetime
import logging
from os import getenv

from mysql.connector import pooling

DB_IP = getenv("DB_IP", "10.33.0.3")
DB_DBNAME = getenv("DB_DBNAME", "ahaz")
DB_USERNAME = getenv("DB_USERNAME", "dbeaver")
DB_PASSWORD = getenv("DB_PASSWORD", "dbeaver")
K8S_IP_RANGE = getenv("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")

logger = logging.getLogger()

pool = None


def get_connection() -> pooling.PooledMySQLConnection:
    global pool
    if pool is None:
        logger.debug("Initializing connection pool")
        pool = pooling.MySQLConnectionPool(
            pool_name="mypool",
            pool_size=10,
            pool_reset_session=True,
            host=DB_IP,
            database=DB_DBNAME,
            user=DB_USERNAME,
            password=DB_PASSWORD,
        )
    return pool.get_connection()


def getUTCasStr() -> str:
    return str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))


def get_challenges_from_db() -> list[str]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT name FROM challenges")
        rows = cursor.fetchall()
    return [str(row[0]) for row in rows]


def get_pods(name: str) -> list[tuple]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT * FROM pods WHERE name = %s", (name,))
        rows = cursor.fetchall()
    return list(rows)


def get_env_vars(k8s_name: str) -> list[dict]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT env_var_name, env_var_value FROM env_vars WHERE k8s_name = %s", (k8s_name,))
        rows = cursor.fetchall()

    env_vars = []
    for i in rows:
        env_vars.append({"name": str(i[0]).upper(), "value": i[1]})
    return env_vars


def get_k8s_name_networks(k8s_name: str) -> list[str]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT netname FROM net_rules WHERE k8s_name = %s", (k8s_name,))
        rows = cursor.fetchall()

    netnames = []
    for i in rows:
        netnames.append(i[0])
    return netnames


def get_unique_networks(challengename: str) -> list[str]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT DISTINCT netname FROM net_rules WHERE name = %s", (challengename,))
        rows = cursor.fetchall()
    return [str(row[0]) for row in rows]


def get_pods_in_network(challengename: str, netname: str) -> list[str]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT k8s_name FROM net_rules WHERE netname = %s AND name = %s", (netname, challengename)
        )
        rows = cursor.fetchall()
    return [str(row[0]) for row in rows]


def get_challenge_from_k8s_name(k8s_name: str) -> str:
    with get_connection() as conn, conn.cursor() as cursor:
        # FIXME: Add limit=1 to the query to avoid fetching unnecessary rows
        cursor.execute("SELECT name FROM pods WHERE k8s_name = %s", (k8s_name,))
        rows = cursor.fetchall()
    return str(rows[0][0])


def insert_team_into_db(teamname: str) -> None:
    with get_connection() as conn, conn.cursor() as cursor:
        if get_team_id(teamname) != "null":
            raise ValueError("team with that name already exists in db")
        cursor.execute("INSERT INTO teams (name) VALUES (%s)", (teamname,))
        conn.commit()


def insert_vpn_port_into_db(teamname: str, port: int) -> str | None:
    if get_team_port(teamname) != "null":
        return "team already has port allocated to it"
    if get_port_team(port) != "null":
        return "port " + str(port) + " is already allocated"
    with get_connection() as conn, conn.cursor() as cursor:
        teamid = get_team_id(teamname)
        cursor.execute("INSERT INTO vpn_map(port,teamid) VALUES (%s, %s)", (port, teamid))
        conn.commit()


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


def insert_user_vpn_config(teamname: str, username: str, config: str) -> None:
    config = str(config).replace("\\n", "\n")
    config = config.replace(
        "<key>", "route-nopull\nroute " + parse_ip_range(K8S_IP_RANGE) + "\n\n<key>"
    )  # add IP route to the config
    config = config.replace("redirect-gateway def1", "")  # remove the rule that replaces all routes with VPN
    config = config + "\ncomp-lzo yes\nallow-compression yes"

    with get_connection() as conn, conn.cursor() as cursor:
        teamid = get_team_id(teamname)
        cursor.execute(
            "INSERT INTO vpn_storage(teamID,username,config) VALUES (%s, %s, %s)", (teamid, username, config)
        )
        conn.commit()


def get_team_id(teamname: str) -> str:
    with get_connection() as conn, conn.cursor() as cursor:
        # FIXME: Add limit=1 to the query to avoid fetching unnecessary rows
        # TODO: implement sanitization here
        cursor.execute("SELECT teamID FROM teams WHERE name=%s", (teamname,))
        rows = cursor.fetchall()

    if len(rows) == 0 or len(rows[0]) == 0:
        return "null"

    return rows[0][0]


def get_team_port(teamname: str) -> str:
    teamID = get_team_id(teamname)
    with get_connection() as conn, conn.cursor() as cursor:
        # FIXME: Add limit=1 to the query to avoid fetching unnecessary rows
        cursor.execute("SELECT port FROM vpn_map WHERE teamID=%s", (teamID,))
        rows = cursor.fetchall()

    if len(rows) == 0 or len(rows[0]) == 0:
        return "null"

    return rows[0][0]


def get_port_team(port: int) -> str:
    with get_connection() as conn, conn.cursor() as cursor:
        # FIXME: Add limit=1 to the query to avoid fetching unnecessary rows
        cursor.execute("SELECT teamID FROM vpn_map WHERE port=%s", (port,))
        rows = cursor.fetchall()

    if len(rows) == 0 or len(rows[0]) == 0:
        return "null"

    return rows[0][0]


def get_user_vpn_config(teamname: str, username: str) -> str:
    teamID = get_team_id(teamname)
    with get_connection() as conn, conn.cursor() as cursor:
        # FIXME: Add limit=1 to the query to avoid fetching unnecessary rows
        cursor.execute("SELECT config FROM vpn_storage WHERE teamID=%s and username=%s", (teamID, username))
        rows = cursor.fetchall()

    if len(rows) == 0 or len(rows[0]) == 0:
        return "null"

    return rows[0][0]


def get_last_port() -> int:
    with get_connection() as conn, conn.cursor() as cursor:
        # FIXME: Add limit=1 to the query to avoid fetching unnecessary rows
        cursor.execute("SELECT port FROM vpn_map ORDER BY port DESC")
        rows = cursor.fetchall()

    return int(rows[0][0])


def delete_team_and_vpn(teamname: str) -> None:
    teamID = get_team_id(teamname)
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE from register_status WHERE name = %s", (teamname,))
        cursor.execute("DELETE FROM vpn_map WHERE teamID = %s", (teamID,))
        cursor.execute("DELETE FROM vpn_storage WHERE teamID = %s", (teamID,))
        cursor.execute("DELETE from teams WHERE teamID = %s", (teamID,))
        conn.commit()


def get_registration_progress_team(teamname: str) -> int:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT state FROM register_status WHERE name='" + teamname + "' ORDER BY state DESC")
        rows = cursor.fetchall()

    if len(rows) == 0 or len(rows[0]) == 0:
        return -999
    return int(rows[0][0])


def get_registration_progress_user(teamname: str, username: str) -> str:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT state FROM register_status WHERE name=%s and user=%s ORDER BY state DESC",
            (teamname, username),
        )
        rows = cursor.fetchall()
    if len(rows) == 0 or len(rows[0]) == 0:
        return "null"
    return rows[0][0]


def set_registration_progress_team(teamname: str, username: str, status: int) -> None:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO register_status (name, user, state, timestamp) VALUES (%s, %s, %s, %s)",
            (teamname, username, status, getUTCasStr()),
        )
        conn.commit()
