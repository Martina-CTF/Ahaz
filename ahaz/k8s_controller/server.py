import asyncio
import json
import logging
import os
import random
from asyncio.subprocess import Process
from threading import Thread
from typing import TypedDict

import db.operator as db
import redis.asyncio as aioredis
import uvicorn
from ahaz_common.task import Task
from k8s_controller.db.collections import init_db
from pydantic import ValidationError
from quart import Quart, Response, make_response, request

from .controller import (
    get_pods_namespace,
    k8s_watcher,
)
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


# # HACK: Test function to add a task definition to the DB
# @app.route("/task", methods=["POST"])
# async def create_task():
#     try:
#         request_data = Task(**await request.get_json())
#     except ValidationError as e:
#         logger.error(f"Validation error: {e}")
#         return "Invalid request data", 400

#     await insert_task_definition(request_data)
#     return "Task created successfully", 201

@app.route("/task", methods=["GET"])
async def get_tasks():
    tasks = await db.list_challenges()
    return json.dumps(tasks), 200, {"Content-Type": "application/json"}

@app.route("/task/<string:id>", methods=["GET"])
async def get_task(id: str):
    try:
        task = await db.get_task_definition(id)
    except ValueError:
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

    if await db.task_definition_exists(id):
        existing_task = await db.get_task_definition(id)
        if existing_task.model_dump_json() == task.model_dump_json():
            return existing_task.model_dump_json(), 200, {"Content-Type": "application/json"}

    await db.insert_task_definition(task)
    # TODO: what happens when a challenge is already running?

    return task.model_dump_json(), 201, {"Content-Type": "application/json"}

@app.route("/task/<string:id>", methods=["DELETE"])
async def delete_task(id: str):
    try:
        await db.delete_task_definition(id)
    except ValueError:
        return {"error": "Task not found"}, 404, {"Content-Type": "application/json"}

    return Response(None, status=204)

@app.route("/team", methods=["GET"])
async def get_teams():
    teams = await db.list_teams()
    return json.dumps(teams), 200, {"Content-Type": "application/json"}

@app.route("/team/<string:id>", methods=["GET"])
async def get_team(id: str):
    try:
        team = await db.get_team(id)
    except ValueError:
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}
    
    return team.model_dump_json(), 200, {"Content-Type": "application/json"}

@app.route("/team/<string:id>", methods=["PUT"])
async def update_team(id: str):
    try:
        team = await db.get_team(id)
        return team.model_dump_json(), 200, {"Content-Type": "application/json"}
    except ValueError:
        pass

    # Get unused port
    used_ports = await db.list_ports()
    random.shuffle(used_ports) # God help me if a port collision actually happens.

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

    team = db.Team(team_id=id, port=port)
    await db.set_team(team)

    # TODO: enqueue work to create namespace, vpn container, expose it, etc.

    return {"message": "Team created successfully"}, 201, {"Content-Type": "application/json"}

@app.route("/team/<string:id>", methods=["DELETE"])
async def delete_team(id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}
    # TODO: delete team namespace, vpn container, and then remove from DB
    return {"error": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/namespace", methods=["GET"])
async def get_team_namespace(id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    show_invisible = request.args.get("show_invisible", "false").lower() == "true"

    try:
        pods = await get_pods_namespace(id, show_invisible)
        return json.dumps(pods), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Unexpected error retrieving pods for team {id}: {e}")
        return {"error": "Error retrieving pods"}, 500, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/namespace/<string:task_id>", methods=["PUT"])
async def start_challenge(id: str, task_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/namespace/<string:task_id>", methods=["DELETE"])
async def stop_task(id: str, task_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/user", methods=["GET"])
async def get_team_users(id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/user/<string:user_id>", methods=["GET"])
async def get_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/user/<string:user_id>", methods=["PUT"])
async def create_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/user/<string:user_id>", methods=["PATCH"])
async def update_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

@app.route("/team/<string:id>/user/<string:user_id>", methods=["DELETE"])
async def delete_user(id: str, user_id: str):
    if not await db.team_exists(id):
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}

    return {"message": "Not implemented"}, 501, {"Content-Type": "application/json"}

# @app.route("/start_challenge", methods=["POST", "GET"])
# async def start_challenge_request():
#     try:
#         request_data = ChallengeRequest(**await request.get_json())
#     except ValidationError as e:
#         logger.error(f"Validation error: {e}")
#         return "Invalid request data", 400

#     logger.info(
#         f"Received start challenge request for challenge {request_data.challenge_id}"
#         + f" from {request_data.team_id}"
#     )

#     try:
#         await start_challenge(request_data.team_id, request_data.challenge_id)
#     except ValueError:
#         return "challenge not found", 404
#     except Exception as e:
#         logger.error(f"Unexpected error starting challenge: {e}")
#         return "error starting challenge", 500

#     return "successfully started challenge", 200


# @app.route("/stop_challenge", methods=["POST", "GET"])
# async def stop_challenge_request():
#     try:
#         request_data = ChallengeRequest(**await request.get_json())
#     except ValidationError as e:
#         logger.error(f"Validation error: {e}")
#         return "Invalid request data", 400

#     logger.info(
#         f"Received stop challenge request for challenge {request_data.challenge_id}"
#         + f" from {request_data.team_id}"
#     )
#     status = stop_challenge(request_data.team_id, request_data.challenge_id)
#     return status


# @app.route("/get_challenges", methods=["GET"])
# async def get_challenges():
#     task_list = await list_challenges()
#     return json.dumps([{"challengename": challenge} for challenge in task_list])  # TODO: the fuck?


# @app.route("/get_pods_namespace", methods=["GET"])
# async def get_pods_namespace_request():
#     try:
#         request_data = TeamRequest(**await request.get_json())
#     except ValidationError as e:
#         logger.error(f"Validation error: {e}")
#         return "Invalid request data", 400

#     logger.info(f"Getting pods for team {request_data.team_id}")
#     try:
#         podresult = await get_pods_namespace(str(request_data.team_id), False)
#     except Exception as e:
#         logger.error(f"Unexpected error retrieving pods: {e}")
#         return "error retrieving pods", 500
#     logger.debug(f"Pods for team {request_data.team_id}:\n{podresult}")
#     return podresult


# @app.route("/get_user", methods=["GET"])
# async def getuser():
#     try:
#         request_data = UserRequest(**await request.get_json())
#     except ValidationError as e:
#         logger.error(f"Validation error: {e}")
#         return "Invalid request data", 400
    
#     try:
#         config = await get_user(
#             request_data.team_id, request_data.user_id, CERT_DIR_CONTAINER + request_data.team_id
#         )
#     except ValueError:
#         logger.info(f"User {request_data.user_id} has no certificate yet.")
#         return "user not found", 404
#     except Exception as e:
#         logger.error(f"Unexpected error retrieving user: {e}")
#         return "error retrieving user", 500
    
#     return config, 200, {"Content-Type": "text/plain"}


# @app.route("/autogenerate", methods=["POST", "GET"])
# async def autogenerate():
#     try:
#         request_data = UserRequest(**await request.get_json())
#     except ValidationError as e:
#         logger.error(f"Validation error: {e}")
#         return "Invalid request data", 400

#     try:
#         port = (
#             TEAM_PORT_RANGE_START + int(request_data.team_id)  # HACK: GOD PLEASE HAVE GOOD INPUTS ONLY
#         )
#     except Exception as e:
#         logger.error(f"Invalid team_id for port calculation: {request_data.team_id}")
#         logger.error(e)
#         port = TEAM_PORT_RANGE_START

#     enqueued_ids = await work_queue.enqueue_many(
#         [
#             Work(
#                 id="gen_cert",
#                 type="gen_cert",
#                 payload={
#                     "team_id": request_data.team_id,
#                     "port": port,
#                     "public_domainname": PUBLIC_DOMAINNAME,
#                     "certdir": CERT_DIR_CONTAINER,
#                 },
#                 idempotent_on={"team_id": request_data.team_id},
#             ),
#             Work(
#                 id="create_namespace",
#                 type="create_namespace",
#                 payload={"team_id": request_data.team_id},
#                 idempotent_on={"team_id": request_data.team_id},
#             ),
#             Work(
#                 id="create_vpn_container",
#                 type="create_vpn_container",
#                 payload={"team_id": request_data.team_id},
#                 idempotent_on={"team_id": request_data.team_id},
#                 deps=["gen_cert", "create_namespace"],
#             ),
#             Work(
#                 id="expose_vpn_container",
#                 type="expose_vpn_container",
#                 payload={"team_id": request_data.team_id, "port": port},
#                 idempotent_on={"team_id": request_data.team_id},
#                 deps=["create_vpn_container"],
#             ),
#             Work(
#                 id="insert_db",
#                 type="insert_db",
#                 payload={"team_id": request_data.team_id, "port": port},
#                 idempotent_on={"team_id": request_data.team_id},
#             ),
#             Work(
#                 id="register_user",
#                 type="register_user",
#                 payload={"team_id": request_data.team_id, "user_id": request_data.user_id},
#                 idempotent_on={"team_id": request_data.team_id, "user_id": request_data.user_id},
#                 deps=["gen_cert"],
#             ),
#         ]
#     )

#     return json.dumps({"status": "enqueued", "tasks": [id for id in enqueued_ids]}), 200


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
        target=lambda: asyncio.new_event_loop().run_until_complete(k8s_watcher(redis_client)),
        daemon=True,
    ).start()

    uvicorn.run("k8s_controller.server:app", host="0.0.0.0", port=5000, workers=4)
