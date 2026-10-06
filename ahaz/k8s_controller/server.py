import asyncio
import json
import logging
import os
import random
from asyncio.subprocess import Process
from threading import Thread
from typing import TypedDict

import redis.asyncio as aioredis
import uvicorn
from ahaz_common.task import Task
from pydantic import ValidationError
from quart import Quart, Response, make_response, request

from . import controller as k8s
from .controller import PodInfo
from .crypto import manager as cert
from .db import operator as db
from .db.collections import init_db
from .work import Work, WorkQueue

CERT_DIR_CONTAINER = os.getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certs/")
PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(os.getenv("TEAM_PORT_RANGE_START", 31200))

app = Quart(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = aioredis.Redis.from_url(REDIS_URL)
work_queue = WorkQueue(redis_client)

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=LOGLEVEL,
    format="[%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger()

# Set kubernetes client logging level to INFO to reduce verbosity
logging.getLogger("kubernetes").setLevel(logging.INFO)
logging.getLogger("mysql").setLevel(logging.INFO)


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200, {"Content-Type": "text/plain"}


@app.route("/task", methods=["GET"])
async def get_tasks():
    tasks = await db.list_challenges()
    return json.dumps(tasks), 200, {"Content-Type": "application/json"}


@app.route("/task/<string:id>", methods=["GET"])
async def get_task(id: str):
    task = await db.get_task_definition(id)
    if task is None:
        return {"error": "Task not found"}, 404, {"Content-Type": "application/json"}

    return task.model_dump_json(), 200, {"Content-Type": "application/json"}


@app.route("/task/<string:id>", methods=["PUT"])
async def update_task(id: str):
    try:
        task = Task(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return {"error": "Invalid request data"}, 400, {"Content-Type": "application/json"}
    except ValueError as e:
        logger.error(f"Value error: {e}")
        return {"error": str(e)}, 400, {"Content-Type": "application/json"}

    existing_task = await db.get_task_definition(id)
    if existing_task is not None and existing_task.model_dump_json() == task.model_dump_json():
        return existing_task.model_dump_json(), 200, {"Content-Type": "application/json"}

    await db.insert_task_definition(task)

    return task.model_dump_json(), 201, {"Content-Type": "application/json"}


@app.route("/task/<string:id>", methods=["DELETE"])
async def delete_task(id: str):
    if not await db.delete_task_definition(id):
        return {"error": "Task not found"}, 404, {"Content-Type": "application/json"}

    return Response(None, status=204)


@app.route("/team", methods=["GET"])
async def get_teams():
    teams = await db.list_teams()
    return json.dumps(teams), 200, {"Content-Type": "application/json"}


async def get_user_raw(team_id: str, user_id: str) -> str | None:
    user_cert = await cert.get_client_ovpn_config(user_id, team_id)
    if user_cert is None:
        return None

    return json.dumps({"id": user_id, "vpn_status": "active", "vpn_config": user_cert})


async def get_team_raw(team_id: str) -> str | None:
    team = await db.get_team(team_id)
    if team is None:
        return None

    namespace_status = "none"
    if k8s.check_namespace_exists(team_id):
        namespace_status = "exists"
    if cert.get_server_cert(team_id) is not None:
        namespace_status = "cert_exists"
    # TODO: check for VPN container?

    team_dict = team.model_dump()
    team_dict["namespace_status"] = namespace_status
    team_dict["users"] = []

    # Remove ta_key
    team_dict.pop("ta_key", None)

    logger.debug(f"team_dict: {team_dict}")

    return json.dumps(team_dict)


@app.route("/team/<string:id>", methods=["GET"])
async def get_team(id: str):
    team_raw = await get_team_raw(id)
    if team_raw is None:
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return team_raw, 200, {"Content-Type": "application/json"}


@app.route("/team/<string:id>", methods=["PUT"])
async def update_team(id: str):
    team_raw = await get_team_raw(id)
    if team_raw is not None:
        return team_raw, 200, {"Content-Type": "application/json"}

    # Get unused port
    used_ports = await db.list_ports()
    random.shuffle(used_ports)  # God help me if a port collision actually happens.

    port = None

    if len(used_ports) == 0:
        port = TEAM_PORT_RANGE_START
    else:
        for used_port in used_ports:
            if used_port < TEAM_PORT_RANGE_START:
                continue

            if used_port + 1 in used_ports:
                continue

            port = used_port + 1
            break

    if port is None:
        return {"error": "No available ports"}, 500, {"Content-Type": "application/json"}

    work_ids = await work_queue.enqueue_many(
        [
            Work(
                id="gen_cert",
                type="gen_cert",
                payload={
                    "team_id": id,
                    "port": port,
                    "public_domainname": PUBLIC_DOMAINNAME,
                    "certdir": CERT_DIR_CONTAINER,
                },
                idempotent_on={"team_id": id},
            ),
            Work(
                id="create_namespace",
                type="create_namespace",
                payload={"team_id": id},
                idempotent_on={"team_id": id},
            ),
            Work(
                id="create_vpn_container",
                type="create_vpn_container",
                payload={"team_id": id},
                idempotent_on={"team_id": id},
                deps=["gen_cert", "create_namespace"],
            ),
            Work(
                id="expose_vpn_container",
                type="expose_vpn_container",
                payload={"team_id": id, "port": port},
                idempotent_on={"team_id": id},
                deps=["create_vpn_container"],
            ),
            Work(
                id="insert_db",
                type="insert_db",
                payload={"team_id": id, "port": port},
                idempotent_on={"team_id": id},
            ),
        ]
    )

    await work_queue.remove_idempotency_many(
        [
            ("delete_team_namespace", {"team_id": id}),
            ("delete_team_certificates", {"team_id": id}),
            ("delete_team_db", {"team_id": id}),
        ]
    )

    logger.debug(f"Enqueued tasks for team {id}: {work_ids}")

    team_raw = await get_team_raw(id)
    if team_raw is None:
        team_raw = json.dumps({"team_id": id, "port": port, "namespace_status": "none", "users": []})

    return team_raw, 201, {"Content-Type": "application/json"}


@app.route("/team/<string:id>", methods=["DELETE"])
async def delete_team(id: str):
    team = await db.get_team(id)
    if team is None:
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    port = team.port

    user_certs = await cert.get_client_certificate_all(id)
    for user_id in user_certs.keys():
        work_id = await work_queue.enqueue(
            Work(
                id="delete_user_certificate",
                type="delete_user_certificate",
                payload={"team_id": id, "user_id": user_id},
                idempotent_on={"team_id": id, "user_id": user_id},
            )
        )
        logger.debug(f"Enqueued delete_user_certificate for team {id} and user {user_id}: {work_id}")

    work_ids = await work_queue.enqueue_many(
        [
            Work(
                id="delete_team_namespace",
                type="delete_team_namespace",
                payload={"team_id": id},
                idempotent_on={"team_id": id},
            ),
            Work(
                id="delete_team_certificates",
                type="delete_team_certificates",
                payload={"team_id": id},
                idempotent_on={"team_id": id},
            ),
            Work(
                id="delete_team_db",
                type="delete_team_db",
                payload={"team_id": id},
                idempotent_on={"team_id": id},
            ),
        ]
    )

    logger.debug(f"Enqueued tasks for deleting team {id}: {work_ids}")

    await work_queue.remove_idempotency_many(
        [
            ("gen_cert", {"team_id": id}),
            ("create_namespace", {"team_id": id}),
            ("create_vpn_container", {"team_id": id}),
            ("expose_vpn_container", {"team_id": id, "port": port}),
            ("insert_db", {"team_id": id, "port": port}),
            *[("register_user", {"team_id": id, "user_id": user_id}) for user_id in user_certs.keys()],
        ]
    )

    return json.dumps({"team_id": id, "status": "deleting"}), 202, {"Content-Type": "application/json"}


@app.route("/team/<string:id>/namespace", methods=["GET"])
async def get_team_namespace(id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    show_invisible = request.args.get("show_invisible", "false").lower() == "true"
    try:
        pods = await k8s.get_pods_namespace(id, show_invisible)
        return json.dumps(pods), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Unexpected error retrieving pods for team {id}: {e}")
        return {"error": "Error retrieving pods"}, 500, {"Content-Type": "application/json"}


def filter_pods_by_task(pods: list[PodInfo], task: str) -> dict:
    pods_filtered = [
        {"name": pod["name"], "status": pod["status"], "ip": pod["ip"], "visible": pod["visibleIP"]}
        for pod in pods
        if pod["task"] == task and pod["status"] != "Terminated"
    ]

    return {
        "task": task,
        "status": "available"
        if len([pod for pod in pods_filtered if pod["status"] != "Running"]) == 0
        else "unavailable",
        "pods": pods_filtered,
    }


@app.route("/team/<string:team>/namespace/<string:task>", methods=["PUT"])
async def start_challenge(team: str, task: str):
    if not await db.team_exists(team):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    if not await db.task_definition_exists(task):
        return {"error": "Task not found"}, 404, {"Content-Type": "application/json"}

    if not k8s.check_namespace_exists(team):
        return {"error": "Team namespace does not exist"}, 400, {"Content-Type": "application/json"}

    pods: list[PodInfo] = await k8s.get_pods_namespace(team, True)
    pods_filtered = filter_pods_by_task(pods, task)

    if len(pods_filtered["pods"]) > 0:
        return json.dumps(pods_filtered), 200, {"Content-Type": "application/json"}

    work_id = await work_queue.enqueue(
        Work(id="start_challenge", type="start_challenge", payload={"team_id": team, "task": task})
    )

    await work_queue.remove_idempotency("stop_challenge", {"team_id": team, "task": task})

    logger.debug(f"Enqueued start_challenge for team {team} and task {task}: {work_id}")

    return json.dumps({"task": task, "status": "starting"}), 202, {"Content-Type": "application/json"}


@app.route("/team/<string:team>/namespace/<string:task>", methods=["DELETE"])
async def stop_task(team: str, task: str):
    if not await db.team_exists(team):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    if not await db.task_definition_exists(task):
        return {"error": "Task not found"}, 404, {"Content-Type": "application/json"}

    if not k8s.check_namespace_exists(team):
        return {"error": "Team namespace does not exist"}, 400, {"Content-Type": "application/json"}

    work_id = await work_queue.enqueue(
        Work(id="stop_challenge", type="stop_challenge", payload={"team_id": team, "task": task})
    )

    await work_queue.remove_idempotency("start_challenge", {"team_id": team, "task": task})

    logger.debug(f"Enqueued stop_challenge for team {team} and task {task}: {work_id}")

    pods = filter_pods_by_task(await k8s.get_pods_namespace(team, True), task)

    return json.dumps(pods), 202, {"Content-Type": "application/json"}


@app.route("/team/<string:id>/user", methods=["GET"])
async def get_team_users(id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    user_certs = await cert.get_client_certificate_all(id)
    users = [
        {"id": user_id, "vpn_status": "active", "vpn_config": await cert.get_client_ovpn_config(user_id, id)}
        for user_id in user_certs.keys()
    ]

    return json.dumps(users), 200, {"Content-Type": "application/json"}


@app.route("/team/<string:id>/user/<string:user_id>", methods=["GET"])
async def get_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    user_raw = await get_user_raw(id, user_id)
    if user_raw is None:
        return {"error": "User not found"}, 404, {"Content-Type": "application/json"}

    return user_raw, 200, {"Content-Type": "application/json"}


@app.route("/team/<string:id>/user/<string:user_id>", methods=["PUT"])
async def create_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    code = 200

    status = await get_user_raw(id, user_id)
    if status is None:
        code = 202
        work_id = await work_queue.enqueue(
            Work(id="register_user", type="register_user", payload={"team_id": id, "user_id": user_id})
        )
        await work_queue.remove_idempotency("delete_user_certificate", {"team_id": id, "user_id": user_id})
        logger.debug(f"Enqueued register_user for team {id} and user {user_id}: {work_id}")

        status = await get_user_raw(id, user_id)
        if status is None:
            status = json.dumps({"id": user_id, "vpn_status": "registering", "vpn_config": None})

    return json.dumps(status), code, {"Content-Type": "application/json"}


@app.route("/team/<string:id>/user/<string:user_id>", methods=["PATCH"])
async def update_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    if not await cert.get_client_certificate(team_id=id, user_id=user_id):
        return {"error": "User not found"}, 404, {"Content-Type": "application/json"}

    status = 200
    request_data = await request.get_json()
    if "vpn_config" in request_data:
        status = 202
        await work_queue.remove_idempotency_many(
            [
                ("delete_user_certificate", {"team_id": id, "user_id": user_id}),
                ("register_user", {"team_id": id, "user_id": user_id}),
            ]
        )
        work_ids = await work_queue.enqueue_many(
            [
                Work(
                    id="delete_user_certificate",
                    type="delete_user_certificate",
                    payload={"team_id": id, "user_id": user_id},
                    idempotent_on={"team_id": id, "user_id": user_id},
                ),
                Work(
                    id="register_user",
                    type="register_user",
                    payload={"team_id": id, "user_id": user_id},
                    idempotent_on={"team_id": id, "user_id": user_id},
                    deps=["delete_user_certificate"],
                ),
            ]
        )
        logger.debug(f"Enqueued tasks for updating user {user_id} in team {id}: {work_ids}")

    return get_user_raw(id, user_id), status, {"Content-Type": "application/json"}


@app.route("/team/<string:id>/user/<string:user_id>", methods=["DELETE"])
async def delete_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    if not await cert.get_client_certificate(team_id=id, user_id=user_id):
        return {"error": "User not found"}, 404, {"Content-Type": "application/json"}

    work_id = await work_queue.enqueue(
        Work(
            id="delete_user_certificate",
            type="delete_user_certificate",
            payload={"team_id": id, "user_id": user_id},
        )
    )
    await work_queue.remove_idempotency("register_user", {"team_id": id, "user_id": user_id})
    logger.debug(f"Enqueued delete_user_certificate for team {id} and user {user_id}: {work_id}")

    return json.dumps({"id": user_id, "status": "deleting"}), 202, {"Content-Type": "application/json"}


@app.route("/events", methods=["GET"])
async def events():
    async def event_stream():
        # Immediate ping to establish connection and send headers
        yield ":keepalive\n\n"
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("ahaz_events")
        while True:
            # pubsub.listen() would work here, but it doesn't timeout and we should send
            # keepalive pings in case the client thinks the connection is dead
            message = await pubsub.get_message(timeout=5)
            if message is None:
                yield ":keepalive\n\n"
                continue

            if message["type"] == "message":
                try:
                    parsed = json.loads(message["data"].decode("utf-8"))
                except Exception as e:
                    logger.error(f"Invalid data provided: {e}")
                    continue
                response = ""
                data = json.dumps(parsed["data"])
                for line in data.splitlines():
                    response += f"data: {line}\n"
                response += f"event: {parsed['type']}\n"
                # We trust that no one else is writing to the Redis publisher and we only write valid JSON
                yield f"{response}\n"

    response = await make_response(
        event_stream(),
        200,
        {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Transfer-Encoding": "chunked",
        },
    )

    # Disable timeout for long-lived connections
    # (trust me, the attribute exists. don't listen to the type checker)
    response.timeout = None  # type: ignore

    return response


@app.before_serving
async def startup():
    logger.info("Initializing database...")
    await init_db()


async def worker_service(worker_count: int):
    class WorkerProcess(TypedDict):
        type: str
        process: Process | None

    processes: list[WorkerProcess] = [{"type": "recovery", "process": None}] + [
        {"type": "worker", "process": None} for _ in range(worker_count)
    ]

    while True:
        # Spawn missing processes
        for p in processes:
            if p["process"] is not None:
                continue
            process_args = ["run", "worker", "--"]
            if p["type"] == "recovery":
                process_args.append("recovery")
            process = await asyncio.create_subprocess_exec("/bin/uv", *process_args, env=os.environ)
            p["process"] = process

        # Wait on any child process to exit
        await asyncio.wait(
            [asyncio.create_task(p["process"].wait()) for p in processes if p["process"] is not None],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check which process died and mark it as None to respawn
        for p in processes:
            process = p["process"]
            if process is not None and process.returncode is not None:
                logger.info(
                    f"{p['type']} process with PID {process.pid} exited with code {process.returncode}"
                )
                p["process"] = None


def main():
    Thread(
        target=lambda: asyncio.new_event_loop().run_until_complete(
            worker_service(int(os.getenv("WORKER_COUNT", 4)))
        ),
        daemon=True,
    ).start()

    # Dedicated thread for Kubernetes watcher
    Thread(
        target=lambda: asyncio.new_event_loop().run_until_complete(k8s.k8s_watcher(redis_client)),
        daemon=True,
    ).start()

    uvicorn.run("k8s_controller.server:app", host="0.0.0.0", port=5000, workers=4)
