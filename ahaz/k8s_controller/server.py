import asyncio
import json
import logging
import os
from threading import Thread

import redis.asyncio as aioredis
import uvicorn
from pydantic import ValidationError
from quart import Quart, make_response, request

from ahaz_common import (
    ChallengeRequest,
    # RegisterTeamRequest,
    TeamRequest,
    UserRequest,
)

from .controller import (
    get_pods_namespace,
    k8s_watcher,
    start_challenge,
    stop_challenge,
)
from .dboperator import (
    get_challenges_from_db,
    get_user_vpn_config,
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


@app.route("/start_challenge", methods=["POST", "GET"])
async def start_challenge_request():
    try:
        request_data = ChallengeRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    logger.info(
        f"Received start challenge request for challenge {request_data.challenge_id}"
        + f" from {request_data.team_id}"
    )
    status = start_challenge(request_data.team_id, request_data.challenge_id)
    if status == 0:
        status = "successfully created challenge"
    return str(status), 200


@app.route("/stop_challenge", methods=["POST", "GET"])
async def stop_challenge_request():
    try:
        request_data = ChallengeRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    logger.info(
        f"Received stop challenge request for challenge {request_data.challenge_id}"
        + f" from {request_data.team_id}"
    )
    status = stop_challenge(request_data.team_id, request_data.challenge_id)
    return status


@app.route("/get_challenges", methods=["GET"])
def get_challenges():
    challenges = get_challenges_from_db()
    return json.dumps([{"challengename": challenge} for challenge in challenges])


@app.route("/get_pods_namespace", methods=["GET"])
async def get_pods_namespace_request():
    try:
        request_data = TeamRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    logger.info(f"Getting pods for team {request_data.team_id}")
    podresult = get_pods_namespace(str(request_data.team_id), False)
    logger.debug(f"Pods for team {request_data.team_id}:\n{podresult}")
    return podresult


@app.route("/get_user", methods=["GET"])
async def getuser():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    return get_user_vpn_config(teamname=request_data.team_id, username=request_data.user_id)


@app.route("/autogenerate", methods=["POST", "GET"])
async def autogenerate():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    try:
        port = (
            TEAM_PORT_RANGE_START + int(request_data.team_id)  # HACK: GOD PLEASE HAVE GOOD INPUTS ONLY
        )
    except Exception as e:
        logger.error(f"Invalid team_id for port calculation: {request_data.team_id}")
        logger.error(e)
        port = TEAM_PORT_RANGE_START

    enqueued_ids = await work_queue.enqueue_many(
        [
            Work(
                id="gen_cert",
                type="gen_cert",
                payload={
                    "team_id": request_data.team_id,
                    "port": port,
                    "public_domainname": PUBLIC_DOMAINNAME,
                    "certdir": CERT_DIR_CONTAINER,
                },
                idempotent_on={"team_id": request_data.team_id},
            ),
            Work(
                id="create_namespace",
                type="create_namespace",
                payload={"team_id": request_data.team_id},
                idempotent_on={"team_id": request_data.team_id},
            ),
            Work(
                id="create_vpn_container",
                type="create_vpn_container",
                payload={"team_id": request_data.team_id},
                idempotent_on={"team_id": request_data.team_id},
                deps=["gen_cert", "create_namespace"],
            ),
            Work(
                id="expose_vpn_container",
                type="expose_vpn_container",
                payload={"team_id": request_data.team_id, "port": port},
                idempotent_on={"team_id": request_data.team_id},
                deps=["create_vpn_container"],
            ),
            Work(
                id="insert_db",
                type="insert_db",
                payload={"team_id": request_data.team_id, "port": port},
                idempotent_on={"team_id": request_data.team_id},
            ),
            Work(
                id="register_user",
                type="register_user",
                payload={"team_id": request_data.team_id, "user_id": request_data.user_id},
                idempotent_on={"team_id": request_data.team_id, "user_id": request_data.user_id},
                deps=["gen_cert"],
            ),
            Work(
                id="insert_user_db",
                type="insert_user_db",
                payload={"team_id": request_data.team_id, "user_id": request_data.user_id},
                idempotent_on={"team_id": request_data.team_id, "user_id": request_data.user_id},
                deps=["register_user"],
            ),
        ]
    )

    return json.dumps({"status": "enqueued", "tasks": [id for id in enqueued_ids]}), 200


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


async def worker_service(worker_count: int):
    import sys

    processes = [{"type": "recovery", "process": None}] + [
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
            process: asyncio.subprocess.Process = p["process"]
            if process is not None and process.returncode is not None:
                logger.info(
                    f"{p['type']} process with PID {process.pid} exited with code {process.returncode}"
                )
                p["process"] = None


def main():
    Thread(
        target=asyncio.new_event_loop().run_until_complete,
        args=(worker_service(int(os.getenv("WORKER_COUNT", 4))),),
        daemon=True,
    ).start()

    # Dedicated thread for Kubernetes watcher
    Thread(
        target=asyncio.new_event_loop().run_until_complete,
        args=(k8s_watcher(redis_client),),
        daemon=True,
    ).start()

    uvicorn.run("k8s_controller.server:app", host="0.0.0.0", port=5000, workers=4)
