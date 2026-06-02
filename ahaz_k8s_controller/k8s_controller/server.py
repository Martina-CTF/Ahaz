import asyncio
import logging
import traceback
from os import getenv
from threading import Thread

import controller
import dboperator
import uvicorn
from pydantic import ValidationError
from quart import Quart, Response, request
from schemas import Task

CERT_DIR_CONTAINER = getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certs/")
PUBLIC_DOMAINNAME = getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(getenv("TEAM_PORT_RANGE_START", 31200))

app = Quart(__name__)

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


@app.route("/task", methods=["GET"])
async def get_tasks():
    tasks = await dboperator.get_tasks()
    return [task.model_dump() for task in tasks], 200, {"Content-Type": "application/json"}


@app.route("/task/<string:id>", methods=["PUT"])
async def update_task(id: str):
    try:
        data = await request.get_json()
        data["name"] = id  # Ensure the task name in the URL is used, not the one in the body)
        # TODO: defaults
        task_request = Task(**data)
        fresh = await dboperator.upsert_task(task_request)
        # TODO: push task_modify event to Redis
        return (
            {"message": "Task upserted successfully"},
            201 if fresh else 200,
            {"Content-Type": "application/json"},
        )
    except ValidationError as e:
        logger.error(f"Invalid task data provided: {e}")
        return {"error": "Invalid task data"}, 400, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error upserting task: {e}\n{traceback.format_exc()}")
        return {"error": "Error upserting task", "message": str(e)}, 500, {"Content-Type": "application/json"}


@app.route("/task/<string:id>", methods=["DELETE"])
async def delete_task(id: str):
    try:
        deleted = await dboperator.delete_task(id)
        if deleted:
            return Response(None, status=204)
            # TODO: push task_delete event to Redis
        else:
            return {"error": "Task not found"}, 404, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error deleting task: {e}\n{traceback.format_exc()}")
        return {"error": "Error deleting task"}, 500, {"Content-Type": "application/json"}


# TODO: /team/* routes: currently return database models, but should return sanitized versions
# without the server's internal VPN config or ports. Should also include namespace status and
# VPN status for the team and users. Resource creation should also start k8s namespace and
# VPN config generation. Deletion should also do stuff.


@app.route("/team", methods=["GET"])
async def get_teams():
    teams = await dboperator.get_all_teams_with_users()
    return [team.model_dump() for team in teams], 200, {"Content-Type": "application/json"}


@app.route("/team/<string:id>", methods=["GET"])
async def get_team(id: str):
    team = await dboperator.get_team_with_users(id)
    if team is None:
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}
    return team.model_dump(), 200, {"Content-Type": "application/json"}


@app.route("/team/<string:id>", methods=["PUT"])
async def get_or_create_team(id: str):
    team = await dboperator.get_team_with_users(id)
    if team is not None:
        return team.model_dump(), 200, {"Content-Type": "application/json"}

    try:
        team = await dboperator.insert_team(id, f"team-{id}", TEAM_PORT_RANGE_START)
        return team.model_dump(), 201, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error creating team: {e}\n{traceback.format_exc()}")
        return {"error": "Error creating team"}, 500, {"Content-Type": "application/json"}


@app.route("/team/<string:id>", methods=["DELETE"])
async def delete_team(id: str):
    try:
        deleted = await dboperator.delete_team(id)
        if deleted:
            return Response(None, status=204)
        else:
            return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error deleting team: {e}\n{traceback.format_exc()}")
        return {"error": "Error deleting team"}, 500, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/user", methods=["GET"])
async def get_users(team_id: str):
    users = await dboperator.get_users(team_id)
    if users is None:
        return {"error": "Team not found"}, 404, {"Content-Type": "application/json"}
    return [user.model_dump() for user in users], 200, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/user/<string:user_id>", methods=["GET"])
async def get_user(team_id: str, user_id: str):
    user = await dboperator.get_user(team_id, user_id)
    if user is None:
        return {"error": "User or team not found"}, 404, {"Content-Type": "application/json"}
    return user.model_dump(), 200, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/user/<string:user_id>", methods=["PUT"])
async def get_or_create_user(team_id: str, user_id: str):
    user = await dboperator.get_user(team_id, user_id)
    if user is not None:
        return user.model_dump(), 200, {"Content-Type": "application/json"}

    try:
        user = await dboperator.insert_user(team_id, user_id)
        return user.model_dump(), 201, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error creating user: {e}\n{traceback.format_exc()}")
        return {"error": "Error creating user"}, 500, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/user/<string:user_id>", methods=["PATCH"])
async def update_user(team_id: str, user_id: str):
    # TODO: event system
    return {"error": "Not implemented"}, 501, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/user/<string:user_id>", methods=["DELETE"])
async def delete_user(team_id: str, user_id: str):
    try:
        deleted = await dboperator.delete_user(team_id, user_id)
        if deleted:
            return Response(None, status=204)
        else:
            return {"error": "User or team not found"}, 404, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error deleting user: {e}\n{traceback.format_exc()}")
        return {"error": "Error deleting user"}, 500, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/namespace", methods=["GET"])
async def get_namespace_status(team_id: str):
    # TODO: need to redo controller.py first
    return {"error": "Not implemented"}, 501, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/namespace/<string:task>", methods=["GET"])
async def start_container(team_id: str, task: str):
    # TODO: need to redo controller.py first, event system
    return {"error": "Not implemented"}, 501, {"Content-Type": "application/json"}


@app.route("/team/<string:team_id>/namespace/<string:task>", methods=["DELETE"])
async def stop_container(team_id: str, task: str):
    # TODO: need to redo controller.py first, event system
    return {"error": "Not implemented"}, 501, {"Content-Type": "application/json"}


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=5000, workers=1)
