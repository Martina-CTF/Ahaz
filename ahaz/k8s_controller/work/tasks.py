from .. import controller
from ..crypto.manager import gen_ta_key, gen_team, generate_user
from ..db.models.team import Team
from ..db.operator import set_team


async def gen_cert(team_id: str):
    await gen_team(team_id)


def create_namespace(team_id: str):
    controller.create_team_namespace(team_id)


async def create_vpn_container(team_id: str):
    await controller.create_team_vpn_container(team_id)


def expose_vpn_container(team_id: str, port: int):
    controller.expose_team_vpn_container(team_id, port)


async def insert_db(team_id: str, port: int):
    ta_key = gen_ta_key()
    team = Team(team_id=team_id, port=port, ta_key=ta_key)
    await set_team(team)


async def register_user(team_id: str, user_id: str):
    await generate_user(team_id, user_id)
