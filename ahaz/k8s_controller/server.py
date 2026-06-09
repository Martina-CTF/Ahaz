import asyncio
import json
import logging
import traceback
from os import getenv
from threading import Thread
from time import sleep

import uvicorn
from ahaz_common import (
    ChallengeRequest,
    RegisterTeamRequest,
    TeamRequest,
    UserRequest,
)
from ahaz_common.task import Task
from k8s_controller.db.collections import init_db
from pydantic import ValidationError
from quart import Quart, make_response, request

from .certmanager import del_team, gen_team, get_user, user_exists
from .controller import (
    create_team_namespace,
    create_team_vpn_container,
    delete_namespace,
    expose_team_vpn_container,
    get_pods_namespace,
    k8s_watcher,
    register_user_ovpn,
    start_challenge,
    stop_challenge,
)
from .db.operator import (
    get_registration_progress_team,
    get_registration_progress_team_any,
    list_challenges,
    set_registration_progress_team,
    set_task_definition,
)
from .events import RedisEventManager

CERT_DIR_CONTAINER = getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certs/")
PUBLIC_DOMAINNAME = getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(getenv("TEAM_PORT_RANGE_START", 31200))

app = Quart(__name__)

REDIS_URL = getenv("REDIS_URL", "redis://localhost:6379")
redis_event_manager = RedisEventManager(REDIS_URL)

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


# HACK: Test function to add a task definition to the DB
@app.route("/task", methods=["POST"])
async def create_task():
    try:
        request_data = Task(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    await set_task_definition(request_data)
    return "Task created successfully", 201


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
async def get_challenges():
    task_list = await list_challenges()
    return json.dumps([{"challengename": challenge} for challenge in task_list])  # TODO: the fuck?


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


async def register_user_threaded(request_data: UserRequest):
    logger.info(f"Registering user {request_data.user_id} to team {request_data.team_id}...")

    logger.debug("About to register user in docker")
    await register_user_ovpn(team_name=request_data.team_id, user_name=request_data.user_id)

    # TODO: Sidestepping DB for now, need to refactor for the logic to be cert-based
    # Might just merge #8 into this branch lmao
    # logger.debug("About to obtain config")
    # config = controller.obtain_user_ovpn_config(teamname=request_data.team_id, username=request_data.user_id)  # noqa: E501

    # logger.debug("About to insert config into db")
    # await insert_user_vpn_config(teamname=request_data.team_id, username=request_data.user_id, config=config)  # noqa: E501
    # logger.debug("Successfully added a user to db")
    return "successfully added a user to db"


@app.route("/add_user", methods=["POST"])
async def adduser():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    teamVPNDirectory = CERT_DIR_CONTAINER + request_data.team_id
    if not await user_exists(request_data.user_id, teamVPNDirectory):
        logger.info(f"User {request_data.user_id} already exists in team {request_data.team_id}")
        return "user already registered", 400

    Thread(target=register_user_threaded, args=(request_data,), daemon=True).start()
    return "Started user creation as a thread"


@app.route("/get_user", methods=["GET"])
async def getuser():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    config = await get_user(
        request_data.team_id, request_data.user_id, CERT_DIR_CONTAINER + request_data.team_id
    )

    if config is None:
        return "user not found", 404
    else:
        return config, 200, {"Content-Type": "text/plain"}


async def gen_team_from_flask_for_subprocess(request_data: RegisterTeamRequest) -> str:
    try:
        logger.debug("doing except")
        await gen_team(
            request_data.team_id,
            request_data.domain_name,
            request_data.port,
            request_data.protocol,
            CERT_DIR_CONTAINER,
        )
        create_team_namespace(request_data.team_id)
        logger.debug("=8")
        await create_team_vpn_container(request_data.team_id)
        logger.debug("about to expose team vpn container")
        expose_team_vpn_container(request_data.team_id, request_data.port)
        # logger.debug("=9")
        # await insert_team_into_db(request_data.team_id)
        # await insert_vpn_port_into_db(request_data.team_id, request_data.port)
        return "Successfully made a team"
    except Exception as e:
        logger.error(f"Error creating team: {e}")
        return "Something went wrong"


@app.route("/gen_team", methods=["POST"])
async def team_post():
    try:
        request_data = RegisterTeamRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    Thread(target=gen_team_from_flask_for_subprocess, args=(request_data,), daemon=True).start()
    logger.info(f"Started team creation as a thread {request_data.team_id}")
    return "Started team creation as a thread"


# TODO: Reduce complexity here
async def autogenerate_subprocess(request_data: UserRequest, port=-1) -> str:  # noqa: C901
    redis_event_mgr = RedisEventManager(REDIS_URL)

    # HACK: Function in function is ugly, but need this working for 21.11.2025 :3
    async def set_registration_progress_threaded(team_id: str, user_id: str, progress: int) -> None:
        await set_registration_progress_team(team_id, user_id, progress)

        await redis_event_mgr.publish_event(
            "ahaz_events",
            json.dumps(
                {
                    "type": "registration_progress",
                    "data": {"team_id": team_id, "user_id": user_id, "progress": progress},
                }
            ),
        )

    if port == -1:
        try:
            port = (
                TEAM_PORT_RANGE_START + int(request_data.team_id)  # HACK: GOD PLEASE HAVE GOOD INPUTS ONLY
            )
        except Exception as e:
            logger.error(f"Invalid team_id for port calculation: {request_data.team_id}")
            logger.error(e)
            port = TEAM_PORT_RANGE_START
    try:
        if await get_registration_progress_team_any(request_data.team_id) == 10:
            return "team is being reregistered"
        logger.debug(await get_registration_progress_team_any(request_data.team_id))
        prog = await get_registration_progress_team_any(request_data.team_id)
        if prog is None:  # if no team has been registered, register it
            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 1)
            logger.debug("started registration proces for a team")

            await gen_team(
                request_data.team_id,
                f"server.{request_data.team_id}.{PUBLIC_DOMAINNAME}",
                port,
                "tcp",
                CERT_DIR_CONTAINER,
            )
            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 2)
            logger.debug(f"generated certificates for team {request_data.team_id}")

            create_team_namespace(request_data.team_id)
            logger.debug(f"created namespace for team {request_data.team_id}")

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 3)
            await create_team_vpn_container(request_data.team_id)
            logger.debug(f"created VPN Container for team {request_data.team_id}")

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 4)
            expose_team_vpn_container(request_data.team_id, port)
            logger.debug(f"exposed VPN Container for team {request_data.team_id}")

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 5)
            logger.debug("mystical 5th step performed")

            # await insert_team_into_db(request_data.team_id)
            # await insert_vpn_port_into_db(request_data.team_id, port)
            logger.debug(f"inserted data into db for team {request_data.team_id}")

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 6)
            logger.info(f"Successfully registered a team {request_data.team_id}")
        elif (
            prog < 6
        ):  # status is less than 6, means that team is being registered, so wait while it is being done
            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 0)

            while prog < 6:
                logger.info(f"waiting for team {request_data.team_id} user {request_data.user_id}")
                sleep(5)
                prog = await get_registration_progress_team_any(request_data.team_id)

                assert prog is not None, "this hack sucks"

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 6)
        elif prog >= 6:  # if team is already registered, then
            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 6)

        teststatus = await get_registration_progress_team(request_data.team_id, request_data.user_id)
        logger.debug(teststatus)
        # I am unsure if this is necessary? Seems to be a non-issue when I comment it out - Tower
        # sleep(2)  # in case the docker container for ovpn file creation is still running and doing something

        if await get_registration_progress_team_any(request_data.team_id) == 10:
            return "team is being reregistered"
        if (await get_registration_progress_team(request_data.team_id, request_data.user_id) is None) or (
            await get_registration_progress_team(request_data.team_id, request_data.user_id) == 6
        ):  # if user isn't registered or this was the user that first called the team registration
            logger.debug("about to register user ovpn config")
            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 7)
            await register_user_ovpn(request_data.team_id, request_data.user_id)

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 8)
            # logger.debug("about to obtain config")
            # config = controller.obtain_user_ovpn_config(request_data.team_id, request_data.user_id)
            # logger.debug("about to insert config into db")
            # await insert_user_vpn_config(request_data.team_id, request_data.user_id, config)

            await set_registration_progress_threaded(request_data.team_id, request_data.user_id, 9)
            logger.debug("successfully added a user to db")
            logger.info(f"Registered user {request_data.user_id} to team {request_data.team_id}")
            return "successfully added a user to db"
        return "Successfuly made a team and registered a user"
    except Exception as e:
        # Print the whole stack trace
        logger.error(traceback.format_exc())
        logger.error(e)
        logger.error(f"ERROR registering a team {request_data.team_id}")
        return "Something went wrong"
    finally:
        await redis_event_mgr.close()


@app.route("/autogenerate", methods=["POST", "GET"])
async def autogenerate():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    status_user = await get_registration_progress_team(request_data.team_id, request_data.user_id)

    if status_user is None:
        # if progress is null, only then start the thread
        Thread(target=asyncio.run, args=(autogenerate_subprocess(request_data),), daemon=True).start()

    status_team = await get_registration_progress_team_any(request_data.team_id)

    if str(status_team) is None:
        status_team = "1"  # set to 1 because thread has possibly just started

    if str(status_user) is None:
        status_user = "1"  # set to 1 because thread has possibly just started

    return json.dumps(
        {
            "team_status": str(status_team),
            "user_status": str(status_user),
        }
    )


async def del_team_subprocess(request_data: UserRequest | TeamRequest, reregister=False) -> None:
    logger.debug(str(request_data.team_id) + " called del_team_subprocess, about to delete namespace")
    delete_namespace(request_data.team_id)
    logger.debug(
        str(request_data.team_id) + " namespace deleted, about to delete team VPN directory for team"
    )
    del_team(request_data.team_id, CERT_DIR_CONTAINER)
    logger.debug(
        str(request_data.team_id) + " cert Directory deleted, about to remove entries of team from db"
    )
    # await delete_team_and_vpn(request_data.team_id)
    logger.debug(str(request_data.team_id) + " entries of team removed from db")

    if reregister:
        if not isinstance(request_data, UserRequest):
            logger.error("Reregister flag set but request_data is not UserRequest")
            return
        Thread(
            target=autogenerate_subprocess,
            args=(request_data, TEAM_PORT_RANGE_START + int(request_data.team_id.replace("a", ""))),
            daemon=True,
        ).start()


# TODO: add token
@app.route("/regenerate", methods=["POST"])
async def regenerate():
    try:
        request_data = UserRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    # teamExists=dboperator.get_team_id(teamname)
    # if(teamExists != "null"):
    #    return "team already exists"

    Thread(target=del_team_subprocess, args=(request_data,), daemon=True).start()
    return f"Started a thread for reregistration of team {request_data.team_id}"


# TODO: add token
@app.route("/del_team", methods=["POST"])
async def del_team_request():
    try:
        request_data = TeamRequest(**await request.get_json())
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return "Invalid request data", 400

    Thread(target=del_team_subprocess, args=(request_data,), daemon=True).start()

    teamname = request_data.team_id
    return f"Started a thread for deletion of team {teamname}"


@app.route("/events", methods=["GET"])
async def events():
    async def event_stream():
        # Immediate ping to establish connection and send headers
        yield ":keepalive\n\n"
        pubsub = redis_event_manager.subscribe()
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


def main():
    Thread(
        # This is an async function, but we are in a thread, so we need to run it in an event loop
        target=asyncio.run,
        args=(k8s_watcher(redis_event_manager),),
        daemon=True,
    ).start()

    uvicorn.run("k8s_controller.server:app", host="0.0.0.0", port=5000, workers=4)
