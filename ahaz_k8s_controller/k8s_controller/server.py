import asyncio
import json
import logging
from os import getenv
from threading import Thread

import controller
import dboperator
import redis.asyncio as aioredis
import uvicorn
from pydantic import ValidationError
from quart import Quart, make_response, request
from work.worker import _recovery_loop, create_tasks

from ahaz_common import (
    ChallengeRequest,
    # RegisterTeamRequest,
    TeamRequest,
    UserRequest,
)

CERT_DIR_CONTAINER = getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certs/")
PUBLIC_DOMAINNAME = getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(getenv("TEAM_PORT_RANGE_START", 31200))

app = Quart(__name__)

REDIS_URL = getenv("REDIS_URL", "redis://localhost:6379")
redis_client = aioredis.Redis.from_url(REDIS_URL)

LOGLEVEL = getenv("LOGLEVEL", "INFO").upper()
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
async def start_challenge():
    try:
        request_data = ChallengeRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    logger.info(
        f"Received start challenge request for challenge {request_data.challenge_id}"
        + f" from {request_data.team_id}"
    )
    status = controller.start_challenge(request_data.team_id, request_data.challenge_id)
    if status == 0:
        status = "successfully created challenge"
    return str(status), 200


@app.route("/stop_challenge", methods=["POST", "GET"])
async def stop_challenge():
    try:
        request_data = ChallengeRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    logger.info(
        f"Received stop challenge request for challenge {request_data.challenge_id}"
        + f" from {request_data.team_id}"
    )
    status = controller.stop_challenge(request_data.team_id, request_data.challenge_id)
    return status


@app.route("/get_challenges", methods=["GET"])
def get_challenges():
    challenges = dboperator.get_challenges_from_db()
    return json.dumps([{"challengename": challenge} for challenge in challenges])


@app.route("/get_pods_namespace", methods=["GET"])
async def get_pods_namespace():
    try:
        request_data = TeamRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    logger.info(f"Getting pods for team {request_data.team_id}")
    podresult = controller.get_pods_namespace(str(request_data.team_id), False)
    logger.debug(f"Pods for team {request_data.team_id}:\n{podresult}")
    return podresult


@app.route("/get_user", methods=["GET"])
async def getuser():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    return dboperator.get_user_vpn_config(teamname=request_data.team_id, username=request_data.user_id)


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

    # TODO: Make idempotent
    await create_tasks(
        redis_client,
        {
            "gen_cert": {
                "payload": {
                    "type": "gen_cert",
                    "team_id": request_data.team_id,
                    "port": port,
                    "public_domainname": PUBLIC_DOMAINNAME,
                    "certdir": CERT_DIR_CONTAINER,
                },
                "deps": [],
            },
            "create_namespace": {
                "payload": {
                    "type": "create_namespace",
                    "team_id": request_data.team_id,
                },
                "deps": [],
            },
            "create_vpn_container": {
                "payload": {
                    "type": "create_vpn_container",
                    "team_id": request_data.team_id,
                },
                "deps": ["gen_cert", "create_namespace"],
            },
            "expose_vpn_container": {
                "payload": {
                    "type": "expose_vpn_container",
                    "team_id": request_data.team_id,
                    "port": port,
                },
                "deps": ["create_vpn_container"],
            },
            "insert_db": {
                "payload": {
                    "type": "insert_db",
                    "team_id": request_data.team_id,
                    "port": port,
                },
                "deps": [],
            },
            "register_user": {
                "payload": {
                    "type": "register_user",
                    "team_id": request_data.team_id,
                    "user_id": request_data.user_id,
                },
                "deps": ["gen_cert"],  # Should have "create_vpn_container"
            },
            "insert_user_db": {
                "payload": {
                    "type": "insert_user_db",
                    "team_id": request_data.team_id,
                    "user_id": request_data.user_id,
                },
                "deps": ["register_user"],
            },
        },
    )

    return "enqueued"


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


if __name__ == "__main__":
    # Spawn worker processes
    import os
    import subprocess
    import sys

    for _ in range(8):
        # These need to inherit our environment
        subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__), "work/worker.py")], env=os.environ
        )

    # Dedicated thread for recovery
    Thread(
        target=asyncio.new_event_loop().run_until_complete,
        args=(_recovery_loop(redis_client),),
        daemon=True,
    ).start()

    # Dedicated thread for Kubernetes watcher
    Thread(
        target=asyncio.new_event_loop().run_until_complete,
        args=(controller.k8s_watcher(redis_client),),
        daemon=True,
    ).start()

    uvicorn.run("server:app", host="0.0.0.0", port=5000, workers=4)
